-- Phase 9C.6: one-time bounded catch-up admission and completion evidence.
-- The accepted pre-capture history gap is explicit and durable; it is never
-- represented as recovered source coverage.

CREATE OR REPLACE FUNCTION rag_prepare_phase9c6_catchup_attempt(
    p_attempt_id TEXT,
    p_collection_name TEXT,
    p_cutoff_sequence BIGINT,
    p_accept_history_gap BOOLEAN,
    p_history_gap_reason TEXT
) RETURNS TABLE (
    attempt_id TEXT,
    decision TEXT,
    batch_cutoff_sequence BIGINT,
    pending_message_count INTEGER,
    runtime_state TEXT,
    runtime_revision BIGINT,
    decision_reasons JSONB,
    qdrant_mutations BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_config rag_incremental_schedule_config%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_pending INTEGER := 0;
    v_latest_capture BIGINT;
    v_healthy_count INTEGER;
    v_unfinished_count INTEGER;
    v_orphan_work INTEGER;
    v_claimed_count INTEGER;
    v_failed_count INTEGER;
    v_missing_work INTEGER;
    v_reasons JSONB := '[]'::jsonb;
    v_decision TEXT;
BEGIN
    IF coalesce(p_attempt_id, '') = ''
       OR coalesce(p_collection_name, '') = ''
       OR p_cutoff_sequence IS NULL
       OR p_cutoff_sequence <= 0 THEN
        RAISE EXCEPTION 'attempt, collection, and positive cutoff are required'
            USING ERRCODE = '22023';
    END IF;
    IF p_accept_history_gap IS DISTINCT FROM true
       OR btrim(coalesce(p_history_gap_reason, '')) = '' THEN
        RAISE EXCEPTION 'the historical gap requires an explicit accepted reason'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_config
    FROM rag_incremental_schedule_config
    WHERE collection_name = p_collection_name
    FOR UPDATE;
    SELECT * INTO v_runtime
    FROM rag_runtime_state
    WHERE collection_name = p_collection_name
    FOR UPDATE;
    IF v_config.collection_name IS NULL OR v_runtime.collection_name IS NULL THEN
        RAISE EXCEPTION 'unknown catch-up collection %', p_collection_name
            USING ERRCODE = 'P0002';
    END IF;

    SELECT max(capture_sequence) INTO v_latest_capture FROM rag_discord_messages;
    SELECT count(*) INTO v_pending FROM rag_pending_chunk_work
    WHERE status = 'pending' AND capture_sequence <= p_cutoff_sequence;
    SELECT count(*) INTO v_healthy_count FROM rag_corpus_versions
    WHERE collection_name = p_collection_name AND status = 'healthy';
    SELECT count(*) INTO v_unfinished_count FROM rag_incremental_runs
    WHERE collection_name = p_collection_name
      AND run_state NOT IN ('completed', 'failed');
    SELECT count(*) INTO v_orphan_work FROM rag_pending_chunk_work w
    LEFT JOIN rag_discord_messages m ON m.message_id = w.source_message_id
    WHERE w.status IN ('pending', 'claimed') AND m.message_id IS NULL;
    SELECT count(*) INTO v_claimed_count FROM rag_pending_chunk_work
    WHERE status = 'claimed';
    SELECT count(*) INTO v_failed_count FROM rag_pending_chunk_work
    WHERE status = 'failed' AND capture_sequence <= p_cutoff_sequence;
    SELECT count(*) INTO v_missing_work FROM rag_discord_messages m
    LEFT JOIN rag_pending_chunk_work w ON w.source_message_id = m.message_id
    WHERE m.capture_sequence <= p_cutoff_sequence AND w.work_id IS NULL;

    IF v_config.schedule_enabled THEN
        v_reasons := v_reasons || jsonb_build_array('schedule_must_remain_disabled');
    END IF;
    IF v_config.catchup_completed THEN
        v_reasons := v_reasons || jsonb_build_array('catchup_already_completed');
    END IF;
    IF v_runtime.runtime_state <> 'serving' THEN
        v_reasons := v_reasons || jsonb_build_array('runtime_not_serving');
    END IF;
    IF v_unfinished_count > 0 THEN
        v_reasons := v_reasons || jsonb_build_array('overlapping_incremental_run');
    END IF;
    IF v_healthy_count <> 1 THEN
        v_reasons := v_reasons || jsonb_build_array('healthy_corpus_count_invalid');
    END IF;
    IF v_orphan_work > 0 OR v_missing_work > 0 THEN
        v_reasons := v_reasons || jsonb_build_array('capture_work_inconsistent');
    END IF;
    IF v_claimed_count > 0 THEN
        v_reasons := v_reasons || jsonb_build_array('claimed_work_present');
    END IF;
    IF v_failed_count > 0 THEN
        v_reasons := v_reasons || jsonb_build_array('failed_work_present');
    END IF;
    IF v_latest_capture IS NULL OR p_cutoff_sequence > v_latest_capture THEN
        v_reasons := v_reasons || jsonb_build_array('cutoff_exceeds_latest_capture');
    END IF;
    IF v_pending = 0 THEN
        v_reasons := v_reasons || jsonb_build_array('no_pending_work');
    END IF;
    IF v_pending > v_config.max_messages_per_run THEN
        v_reasons := v_reasons || jsonb_build_array('message_limit_exceeded');
    END IF;
    v_decision := CASE WHEN jsonb_array_length(v_reasons) = 0
        THEN 'planning' ELSE 'blocked' END;

    INSERT INTO rag_incremental_schedule_attempts (
        attempt_id, collection_name, trigger_source, decision,
        schedule_enabled, catchup_completed, batch_cutoff_sequence,
        pending_message_count, qdrant_mutations, decision_reasons, report
    ) VALUES (
        p_attempt_id, p_collection_name, 'manual_execute', v_decision,
        v_config.schedule_enabled, v_config.catchup_completed,
        p_cutoff_sequence, v_pending, false, v_reasons,
        jsonb_build_object(
            'phase', '9C.6',
            'accepted_history_gap', true,
            'history_gap_reason', p_history_gap_reason,
            'latest_capture_sequence_at_admission', v_latest_capture,
            'runtime_revision_at_admission', v_runtime.state_revision
        )
    ) ON CONFLICT ON CONSTRAINT rag_incremental_schedule_attempts_pkey
      DO UPDATE SET updated_at = now();

    RETURN QUERY SELECT p_attempt_id, v_decision, p_cutoff_sequence,
        v_pending, v_runtime.runtime_state, v_runtime.state_revision,
        v_reasons, false;
END
$$;

CREATE OR REPLACE FUNCTION rag_complete_phase9c6_catchup(
    p_attempt_id TEXT,
    p_run_id TEXT,
    p_evidence JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (
    attempt_id TEXT,
    decision TEXT,
    incremental_run_id TEXT,
    catchup_completed BOOLEAN,
    schedule_enabled BOOLEAN,
    runtime_state TEXT,
    runtime_revision BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_attempt rag_incremental_schedule_attempts%ROWTYPE;
    v_run rag_incremental_runs%ROWTYPE;
    v_config rag_incremental_schedule_config%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_unexplained INTEGER;
    v_deferred_pending INTEGER;
BEGIN
    IF jsonb_typeof(p_evidence) <> 'object' THEN
        RAISE EXCEPTION 'catch-up evidence must be an object'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_attempt FROM rag_incremental_schedule_attempts
    WHERE rag_incremental_schedule_attempts.attempt_id = p_attempt_id
    FOR UPDATE;
    SELECT * INTO v_run FROM rag_incremental_runs
    WHERE rag_incremental_runs.incremental_run_id = p_run_id
    FOR UPDATE;
    SELECT * INTO v_config FROM rag_incremental_schedule_config
    WHERE collection_name = v_attempt.collection_name
    FOR UPDATE;
    SELECT * INTO v_runtime FROM rag_runtime_state
    WHERE collection_name = v_attempt.collection_name
    FOR UPDATE;

    IF v_attempt.attempt_id IS NULL
       OR v_attempt.decision <> 'dispatched'
       OR v_attempt.report->>'phase' <> '9C.6'
       OR coalesce((v_attempt.report->>'accepted_history_gap')::boolean, false) IS NOT TRUE THEN
        RAISE EXCEPTION 'attempt is not an admitted Phase 9C.6 catch-up'
            USING ERRCODE = '55000';
    END IF;
    IF v_run.incremental_run_id IS NULL
       OR v_run.plan_id <> v_attempt.plan_id
       OR v_run.run_state <> 'completed'
       OR v_run.structural_verification_result <> 'passed'
       OR v_run.regression_result <> 'passed'
       OR v_run.corpus_version_after IS NULL
       OR v_run.snapshot_uri NOT LIKE '%qdrant://%' THEN
        RAISE EXCEPTION 'catch-up run lacks completed snapshot and validation evidence'
            USING ERRCODE = '55000';
    END IF;
    IF v_runtime.runtime_state <> 'serving'
       OR v_runtime.active_incremental_run_id IS NOT NULL
       OR v_config.schedule_enabled THEN
        RAISE EXCEPTION 'runtime must be serving with schedule disabled'
            USING ERRCODE = '55000';
    END IF;
    UPDATE rag_pending_chunk_work w SET
        failure_reason = 'phase9c6_deferred_until_future_context'
    WHERE w.status = 'pending'
      AND w.capture_sequence <= v_attempt.batch_cutoff_sequence
      AND w.source_message_id IN (
          SELECT unnest(g.source_message_ids)
          FROM rag_chunk_replacement_plan_groups g
          WHERE g.plan_id = v_run.plan_id AND g.status = 'deferred'
      );
    SELECT count(*) INTO v_deferred_pending FROM rag_pending_chunk_work
    WHERE capture_sequence <= v_attempt.batch_cutoff_sequence
      AND status = 'pending'
      AND failure_reason = 'phase9c6_deferred_until_future_context';
    SELECT count(*) INTO v_unexplained FROM rag_pending_chunk_work
    WHERE capture_sequence <= v_attempt.batch_cutoff_sequence
      AND (
          status = 'claimed'
          OR (status = 'pending'
              AND failure_reason IS DISTINCT FROM
                  'phase9c6_deferred_until_future_context')
      );
    IF v_unexplained <> 0
       OR v_deferred_pending <> v_run.deferred_message_count
       OR v_run.processed_message_count + v_run.deferred_message_count
          <> v_attempt.pending_message_count THEN
        RAISE EXCEPTION 'pre-cutoff work is not fully reconciled'
            USING ERRCODE = '55000';
    END IF;

    UPDATE rag_incremental_schedule_attempts SET
        decision = 'completed', incremental_run_id = p_run_id,
        qdrant_mutations = true,
        report = report || p_evidence || jsonb_build_object(
            'completed_corpus_version', v_run.corpus_version_after,
            'processed_message_count', v_run.processed_message_count,
            'deferred_message_count', v_run.deferred_message_count,
            'snapshot_uri', v_run.snapshot_uri
        ),
        completed_at = now(), updated_at = now()
    WHERE rag_incremental_schedule_attempts.attempt_id = p_attempt_id;
    UPDATE rag_incremental_schedule_config SET
        catchup_completed = true,
        config_metadata = config_metadata || jsonb_build_object(
            'phase9c6_attempt_id', p_attempt_id,
            'phase9c6_incremental_run_id', p_run_id,
            'phase9c6_cutoff_sequence', v_attempt.batch_cutoff_sequence,
            'phase9c6_completed_at', now(),
            'accepted_history_gap', true,
            'history_gap_reason', v_attempt.report->>'history_gap_reason'
        ),
        updated_at = now()
    WHERE collection_name = v_attempt.collection_name
    RETURNING * INTO v_config;

    RETURN QUERY SELECT p_attempt_id, 'completed'::text, p_run_id,
        v_config.catchup_completed, v_config.schedule_enabled,
        v_runtime.runtime_state, v_runtime.state_revision;
END
$$;
