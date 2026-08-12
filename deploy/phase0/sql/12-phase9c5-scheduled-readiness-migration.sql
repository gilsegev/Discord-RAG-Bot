-- Phase 9C.5: disabled-by-default scheduled-operation control, durable
-- attempts/reports, and a deduplicated operator alert outbox.

CREATE TABLE IF NOT EXISTS rag_incremental_schedule_config (
    collection_name TEXT PRIMARY KEY REFERENCES rag_runtime_state(collection_name),
    schedule_enabled BOOLEAN NOT NULL DEFAULT false,
    catchup_completed BOOLEAN NOT NULL DEFAULT false,
    cron_expression TEXT NOT NULL DEFAULT '0 3 * * *',
    schedule_timezone TEXT NOT NULL DEFAULT 'UTC',
    max_messages_per_run INTEGER NOT NULL DEFAULT 500 CHECK (max_messages_per_run BETWEEN 1 AND 10000),
    max_replacement_points INTEGER NOT NULL DEFAULT 1000 CHECK (max_replacement_points BETWEEN 1 AND 10000),
    max_estimated_seconds NUMERIC(12,3) NOT NULL DEFAULT 900 CHECK (max_estimated_seconds > 0),
    max_maintenance_seconds INTEGER NOT NULL DEFAULT 900 CHECK (max_maintenance_seconds BETWEEN 30 AND 3600),
    success_alerts_enabled BOOLEAN NOT NULL DEFAULT true,
    config_metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config_metadata)='object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_incremental_schedule_attempts (
    attempt_id TEXT PRIMARY KEY,
    collection_name TEXT NOT NULL REFERENCES rag_incremental_schedule_config(collection_name),
    trigger_source TEXT NOT NULL CHECK (trigger_source IN ('scheduled','manual_dry_run','manual_execute')),
    decision TEXT NOT NULL CHECK (decision IN ('disabled','blocked','no_work','planning','ready','dispatched','completed','failed')),
    schedule_enabled BOOLEAN NOT NULL,
    catchup_completed BOOLEAN NOT NULL,
    batch_cutoff_sequence BIGINT,
    pending_message_count INTEGER NOT NULL DEFAULT 0 CHECK (pending_message_count >= 0),
    plan_id TEXT REFERENCES rag_chunk_replacement_plans(plan_id),
    incremental_run_id TEXT REFERENCES rag_incremental_runs(incremental_run_id),
    qdrant_mutations BOOLEAN NOT NULL DEFAULT false,
    decision_reasons JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(decision_reasons)='array'),
    report JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(report)='object'),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_incremental_schedule_attempts_recent
    ON rag_incremental_schedule_attempts(collection_name, started_at DESC);

CREATE TABLE IF NOT EXISTS rag_incremental_operator_alerts (
    alert_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id TEXT REFERENCES rag_incremental_schedule_attempts(attempt_id),
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    alert_code TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    alert_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(alert_payload)='object'),
    delivery_status TEXT NOT NULL DEFAULT 'queued' CHECK (delivery_status IN ('queued','sent','failed','suppressed')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    last_error TEXT
);

INSERT INTO rag_incremental_schedule_config (collection_name)
SELECT collection_name FROM rag_runtime_state
ON CONFLICT (collection_name) DO NOTHING;

CREATE OR REPLACE FUNCTION rag_prepare_incremental_schedule_attempt(
    p_attempt_id TEXT,
    p_collection_name TEXT DEFAULT 'tpm_unite_history',
    p_trigger_source TEXT DEFAULT 'scheduled'
) RETURNS TABLE (
    attempt_id TEXT, decision TEXT, schedule_enabled BOOLEAN,
    catchup_completed BOOLEAN, batch_cutoff_sequence BIGINT,
    pending_message_count INTEGER, runtime_state TEXT,
    runtime_revision BIGINT, decision_reasons JSONB,
    qdrant_mutations BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_config rag_incremental_schedule_config%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_cutoff BIGINT;
    v_pending INTEGER := 0;
    v_reasons JSONB := '[]'::jsonb;
    v_decision TEXT;
    v_healthy_count INTEGER;
    v_unfinished_count INTEGER;
    v_orphan_work INTEGER;
BEGIN
    IF coalesce(p_attempt_id,'')='' OR p_trigger_source NOT IN ('scheduled','manual_dry_run','manual_execute') THEN
        RAISE EXCEPTION 'valid attempt ID and trigger source are required' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_config FROM rag_incremental_schedule_config
    WHERE collection_name=p_collection_name FOR UPDATE;
    SELECT * INTO v_runtime FROM rag_runtime_state
    WHERE collection_name=p_collection_name FOR UPDATE;
    IF NOT FOUND OR v_config.collection_name IS NULL THEN
        RAISE EXCEPTION 'unknown schedule collection %',p_collection_name USING ERRCODE='P0002';
    END IF;

    SELECT count(*) INTO v_healthy_count FROM rag_corpus_versions
    WHERE collection_name=p_collection_name AND status='healthy';
    SELECT count(*) INTO v_unfinished_count FROM rag_incremental_runs
    WHERE collection_name=p_collection_name AND run_state NOT IN ('completed','failed');
    SELECT count(*) INTO v_orphan_work FROM rag_pending_chunk_work w
    LEFT JOIN rag_discord_messages m ON m.message_id=w.source_message_id
    WHERE w.status IN ('pending','claimed') AND m.message_id IS NULL;
    SELECT count(*),max(capture_sequence) INTO v_pending,v_cutoff FROM (
        SELECT capture_sequence FROM rag_pending_chunk_work
        WHERE status='pending' ORDER BY capture_sequence
        LIMIT v_config.max_messages_per_run
    ) bounded;

    IF v_runtime.runtime_state <> 'serving' THEN v_reasons:=v_reasons||jsonb_build_array('runtime_not_serving'); END IF;
    IF v_unfinished_count > 0 THEN v_reasons:=v_reasons||jsonb_build_array('overlapping_incremental_run'); END IF;
    IF v_healthy_count <> 1 THEN v_reasons:=v_reasons||jsonb_build_array('healthy_corpus_count_invalid'); END IF;
    IF v_orphan_work > 0 THEN v_reasons:=v_reasons||jsonb_build_array('capture_work_orphaned'); END IF;
    IF NOT v_config.catchup_completed THEN v_reasons:=v_reasons||jsonb_build_array('phase9c6_catchup_required'); END IF;
    IF p_trigger_source='scheduled' AND NOT v_config.schedule_enabled THEN
        v_reasons:=v_reasons||jsonb_build_array('schedule_disabled');
        v_decision:='disabled';
    ELSIF jsonb_array_length(v_reasons)>0 THEN
        v_decision:='blocked';
    ELSIF v_pending=0 THEN
        v_decision:='no_work';
    ELSE
        v_decision:='planning';
    END IF;

    INSERT INTO rag_incremental_schedule_attempts(
        attempt_id,collection_name,trigger_source,decision,schedule_enabled,
        catchup_completed,batch_cutoff_sequence,pending_message_count,
        qdrant_mutations,decision_reasons,report
    ) VALUES (
        p_attempt_id,p_collection_name,p_trigger_source,v_decision,
        v_config.schedule_enabled,v_config.catchup_completed,v_cutoff,v_pending,
        false,v_reasons,jsonb_build_object('runtime_state',v_runtime.runtime_state,'runtime_revision',v_runtime.state_revision)
    ) ON CONFLICT ON CONSTRAINT rag_incremental_schedule_attempts_pkey
      DO UPDATE SET updated_at=now();

    RETURN QUERY SELECT p_attempt_id,v_decision,v_config.schedule_enabled,
        v_config.catchup_completed,v_cutoff,v_pending,v_runtime.runtime_state,
        v_runtime.state_revision,v_reasons,false;
END
$$;

CREATE OR REPLACE FUNCTION rag_attach_incremental_schedule_plan(
    p_attempt_id TEXT,
    p_plan_id TEXT
) RETURNS TABLE (attempt_id TEXT, decision TEXT, plan_id TEXT, decision_reasons JSONB)
LANGUAGE plpgsql
AS $$
DECLARE
    v_attempt rag_incremental_schedule_attempts%ROWTYPE;
    v_config rag_incremental_schedule_config%ROWTYPE;
    v_plan rag_chunk_replacement_plans%ROWTYPE;
    v_current rag_corpus_versions%ROWTYPE;
    v_reasons JSONB := '[]'::jsonb;
    v_decision TEXT;
BEGIN
    SELECT * INTO v_attempt FROM rag_incremental_schedule_attempts WHERE rag_incremental_schedule_attempts.attempt_id=p_attempt_id FOR UPDATE;
    IF NOT FOUND OR v_attempt.decision <> 'planning' THEN RAISE EXCEPTION 'attempt is not planning' USING ERRCODE='55000'; END IF;
    SELECT * INTO v_config FROM rag_incremental_schedule_config WHERE collection_name=v_attempt.collection_name;
    SELECT * INTO v_plan FROM rag_chunk_replacement_plans WHERE rag_chunk_replacement_plans.plan_id=p_plan_id;
    SELECT * INTO v_current FROM rag_corpus_versions WHERE collection_name=v_attempt.collection_name AND status='healthy';
    IF v_plan.plan_id IS NULL OR v_plan.status <> 'shadow_validated' THEN v_reasons:=v_reasons||jsonb_build_array('plan_not_shadow_validated'); END IF;
    IF v_plan.source_corpus_version_id IS DISTINCT FROM v_current.corpus_version_id OR v_plan.source_manifest_digest IS DISTINCT FROM v_current.manifest_digest THEN v_reasons:=v_reasons||jsonb_build_array('plan_source_is_stale'); END IF;
    IF v_plan.batch_cutoff_sequence IS DISTINCT FROM v_attempt.batch_cutoff_sequence THEN v_reasons:=v_reasons||jsonb_build_array('plan_cutoff_mismatch'); END IF;
    IF v_plan.pending_message_count > v_config.max_messages_per_run THEN v_reasons:=v_reasons||jsonb_build_array('message_limit_exceeded'); END IF;
    IF v_plan.replacement_point_count > v_config.max_replacement_points THEN v_reasons:=v_reasons||jsonb_build_array('replacement_limit_exceeded'); END IF;
    IF coalesce(v_plan.estimated_seconds,0) > v_config.max_estimated_seconds THEN v_reasons:=v_reasons||jsonb_build_array('duration_budget_exceeded'); END IF;
    v_decision:=CASE WHEN jsonb_array_length(v_reasons)=0 THEN 'ready' ELSE 'blocked' END;
    UPDATE rag_incremental_schedule_attempts SET plan_id=p_plan_id,decision=v_decision,
        decision_reasons=v_reasons,updated_at=now() WHERE rag_incremental_schedule_attempts.attempt_id=p_attempt_id;
    RETURN QUERY SELECT p_attempt_id,v_decision,p_plan_id,v_reasons;
END
$$;

CREATE OR REPLACE FUNCTION rag_finish_incremental_schedule_attempt(
    p_attempt_id TEXT,
    p_decision TEXT,
    p_run_id TEXT DEFAULT NULL,
    p_qdrant_mutations BOOLEAN DEFAULT false,
    p_report JSONB DEFAULT '{}'::jsonb
) RETURNS rag_incremental_schedule_attempts
LANGUAGE plpgsql
AS $$
DECLARE v_attempt rag_incremental_schedule_attempts%ROWTYPE;
BEGIN
    IF p_decision NOT IN ('disabled','blocked','no_work','ready','dispatched','completed','failed') OR jsonb_typeof(p_report)<>'object' THEN
        RAISE EXCEPTION 'invalid attempt outcome' USING ERRCODE='22023';
    END IF;
    UPDATE rag_incremental_schedule_attempts SET decision=p_decision,
        incremental_run_id=coalesce(p_run_id,incremental_run_id),
        qdrant_mutations=p_qdrant_mutations,report=report||p_report,
        completed_at=CASE WHEN p_decision IN ('disabled','blocked','no_work','completed','failed') THEN now() ELSE completed_at END,
        updated_at=now() WHERE attempt_id=p_attempt_id RETURNING * INTO v_attempt;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown attempt %',p_attempt_id USING ERRCODE='P0002'; END IF;
    RETURN v_attempt;
END
$$;

CREATE OR REPLACE FUNCTION rag_cancel_incremental_drain(
    p_run_id TEXT,
    p_reason TEXT
) RETURNS TABLE (incremental_run_id TEXT,run_state TEXT,runtime_state TEXT,runtime_revision BIGINT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM rag_incremental_runs WHERE rag_incremental_runs.incremental_run_id=p_run_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown incremental run %',p_run_id USING ERRCODE='P0002'; END IF;
    SELECT * INTO v_runtime FROM rag_runtime_state WHERE collection_name=v_run.collection_name FOR UPDATE;
    IF v_run.run_state='failed' AND v_runtime.runtime_state='serving' THEN
        RETURN QUERY SELECT p_run_id,v_run.run_state,v_runtime.runtime_state,v_runtime.state_revision;
        RETURN;
    END IF;
    IF v_run.run_state<>'draining' OR v_runtime.runtime_state<>'draining' OR v_runtime.active_incremental_run_id<>p_run_id THEN
        RAISE EXCEPTION 'run does not own drain' USING ERRCODE='55000';
    END IF;
    UPDATE rag_incremental_runs r SET run_state='failed',failure_step='drain',failure_reason=p_reason,
        completed_at=now(),updated_at=now() WHERE r.incremental_run_id=p_run_id;
    UPDATE rag_runtime_state rs SET runtime_state='serving',active_incremental_run_id=NULL,
        state_revision=rs.state_revision+1,changed_at=now() WHERE rs.collection_name=v_run.collection_name
        RETURNING rs.* INTO v_runtime;
    RETURN QUERY SELECT p_run_id,'failed'::text,v_runtime.runtime_state,v_runtime.state_revision;
END
$$;

CREATE OR REPLACE FUNCTION rag_queue_incremental_alert(
    p_attempt_id TEXT,p_severity TEXT,p_alert_code TEXT,p_dedupe_key TEXT,p_payload JSONB DEFAULT '{}'::jsonb
) RETURNS rag_incremental_operator_alerts
LANGUAGE plpgsql
AS $$
DECLARE v_alert rag_incremental_operator_alerts%ROWTYPE;
BEGIN
    IF p_severity NOT IN ('info','warning','critical') OR coalesce(p_alert_code,'')='' OR coalesce(p_dedupe_key,'')='' OR jsonb_typeof(p_payload)<>'object' THEN
        RAISE EXCEPTION 'invalid alert request' USING ERRCODE='22023';
    END IF;
    INSERT INTO rag_incremental_operator_alerts(attempt_id,severity,alert_code,dedupe_key,alert_payload)
    VALUES(p_attempt_id,p_severity,p_alert_code,p_dedupe_key,p_payload)
    ON CONFLICT(dedupe_key) DO UPDATE SET dedupe_key=EXCLUDED.dedupe_key
    RETURNING * INTO v_alert;
    RETURN v_alert;
END
$$;

CREATE OR REPLACE VIEW rag_incremental_run_reports AS
SELECT a.attempt_id,a.trigger_source,a.decision,a.schedule_enabled,a.catchup_completed,
       a.batch_cutoff_sequence,a.pending_message_count,a.plan_id,a.incremental_run_id,
       a.qdrant_mutations,a.decision_reasons,a.started_at,a.completed_at,
       r.run_state,r.processed_message_count,r.deferred_message_count,
       r.old_point_count,r.replacement_point_count,r.new_point_count,
       r.deleted_point_count,r.phase_durations,r.regression_result,
       r.rollback_status,r.failure_step,r.failure_reason,r.snapshot_uri,
       r.corpus_version_before,r.corpus_version_after,
       rs.runtime_state,rs.state_revision,a.report
FROM rag_incremental_schedule_attempts a
LEFT JOIN rag_incremental_runs r ON r.incremental_run_id=a.incremental_run_id
JOIN rag_runtime_state rs ON rs.collection_name=a.collection_name;
