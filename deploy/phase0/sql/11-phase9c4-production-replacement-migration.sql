-- Phase 9C.4: maintenance admission, replacement snapshots, and guarded
-- production lifecycle transitions. Safe to rerun.

CREATE TABLE IF NOT EXISTS rag_incremental_point_snapshots (
    incremental_run_id TEXT NOT NULL
        REFERENCES rag_incremental_runs(incremental_run_id) ON DELETE CASCADE,
    point_id TEXT NOT NULL,
    vector JSONB NOT NULL CHECK (jsonb_typeof(vector) = 'array'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retained_until TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (incremental_run_id, point_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_incremental_snapshots_retention
    ON rag_incremental_point_snapshots(retained_until);

CREATE TABLE IF NOT EXISTS rag_incremental_manifest_snapshots (
    incremental_run_id TEXT NOT NULL
        REFERENCES rag_incremental_runs(incremental_run_id) ON DELETE CASCADE,
    point_id TEXT NOT NULL,
    manifest_row JSONB NOT NULL CHECK (jsonb_typeof(manifest_row) = 'object'),
    ownership_rows JSONB NOT NULL CHECK (jsonb_typeof(ownership_rows) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retained_until TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (incremental_run_id, point_id)
);

ALTER TABLE rag_incremental_runs
    ADD COLUMN IF NOT EXISTS baseline_regression_run_id TEXT,
    ADD COLUMN IF NOT EXISTS baseline_regression_result TEXT,
    ADD COLUMN IF NOT EXISTS structural_verification_result TEXT;

CREATE OR REPLACE FUNCTION rag_mark_incremental_replacing(
    p_run_id TEXT,
    p_expected_runtime_revision BIGINT
) RETURNS TABLE (
    incremental_run_id TEXT,
    run_state TEXT,
    runtime_state TEXT,
    runtime_revision BIGINT,
    claimed_message_count INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_claimed INTEGER;
BEGIN
    SELECT * INTO v_run FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %', p_run_id USING ERRCODE='P0002';
    END IF;
    SELECT * INTO v_runtime FROM rag_runtime_state
    WHERE collection_name = v_run.collection_name FOR UPDATE;

    IF v_run.run_state = 'replacing'
       AND v_runtime.runtime_state = 'maintenance'
       AND v_runtime.active_incremental_run_id = p_run_id THEN
        RETURN QUERY SELECT p_run_id, v_run.run_state, v_runtime.runtime_state,
            v_runtime.state_revision, v_run.claimed_message_count;
        RETURN;
    END IF;
    IF v_run.run_state <> 'maintenance'
       OR v_runtime.runtime_state <> 'maintenance'
       OR v_runtime.active_incremental_run_id <> p_run_id
       OR v_runtime.state_revision <> p_expected_runtime_revision THEN
        RAISE EXCEPTION 'run does not own maintenance at expected revision'
            USING ERRCODE='40001';
    END IF;

    UPDATE rag_pending_chunk_work w
    SET status='claimed', claimed_at=coalesce(w.claimed_at, now()),
        failure_reason=NULL
    WHERE w.status='pending'
      AND w.capture_sequence <= v_run.batch_cutoff_sequence
      AND w.source_message_id IN (
          SELECT unnest(g.source_message_ids)
          FROM rag_chunk_replacement_plan_groups g
          WHERE g.plan_id=v_run.plan_id AND g.status='ready'
      );
    GET DIAGNOSTICS v_claimed = ROW_COUNT;

    UPDATE rag_incremental_runs r
    SET run_state='replacing', claimed_message_count=v_claimed,
        updated_at=now()
    WHERE r.incremental_run_id=p_run_id
    RETURNING r.* INTO v_run;
    RETURN QUERY SELECT p_run_id, v_run.run_state, v_runtime.runtime_state,
        v_runtime.state_revision, v_run.claimed_message_count;
END
$$;

CREATE OR REPLACE FUNCTION rag_record_incremental_regression(
    p_run_id TEXT,
    p_regression_run_id TEXT,
    p_result TEXT,
    p_is_baseline BOOLEAN DEFAULT false
) RETURNS TABLE (
    incremental_run_id TEXT,
    run_state TEXT,
    runtime_state TEXT,
    runtime_revision BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_regression rag_regression_runs%ROWTYPE;
BEGIN
    IF p_result NOT IN ('passed','failed') OR coalesce(p_regression_run_id,'')='' THEN
        RAISE EXCEPTION 'regression ID and passed/failed result are required'
            USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM rag_incremental_runs r
    WHERE r.incremental_run_id=p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %', p_run_id USING ERRCODE='P0002';
    END IF;
    SELECT * INTO v_runtime FROM rag_runtime_state
    WHERE collection_name=v_run.collection_name FOR UPDATE;
    SELECT * INTO v_regression FROM rag_regression_runs
    WHERE run_id=p_regression_run_id;
    IF NOT FOUND OR v_regression.status <> 'completed' OR v_regression.case_count <> 48 THEN
        RAISE EXCEPTION 'regression run is missing or incomplete'
            USING ERRCODE='55000';
    END IF;
    IF p_result='passed'
       AND (v_regression.pass_count <> 43
            OR v_regression.fail_count <> 1
            OR v_regression.review_count <> 4) THEN
        RAISE EXCEPTION 'regression does not match accepted 43/1/4 baseline'
            USING ERRCODE='55000';
    END IF;

    IF p_is_baseline THEN
        IF v_run.run_state <> 'created' OR v_runtime.runtime_state <> 'serving' THEN
            RAISE EXCEPTION 'baseline regression must be recorded before drain'
                USING ERRCODE='55000';
        END IF;
        UPDATE rag_incremental_runs r SET
            baseline_regression_run_id=p_regression_run_id,
            baseline_regression_result=p_result,
            updated_at=now()
        WHERE r.incremental_run_id=p_run_id RETURNING r.* INTO v_run;
    ELSE
        IF v_run.run_state <> 'validating'
           OR v_runtime.runtime_state <> 'maintenance'
           OR v_runtime.active_incremental_run_id <> p_run_id THEN
            RAISE EXCEPTION 'post-replacement regression requires owned validation'
                USING ERRCODE='55000';
        END IF;
        UPDATE rag_incremental_runs r SET
            regression_run_id=p_regression_run_id,
            regression_result=p_result,
            updated_at=now()
        WHERE r.incremental_run_id=p_run_id RETURNING r.* INTO v_run;
    END IF;
    RETURN QUERY SELECT p_run_id, v_run.run_state, v_runtime.runtime_state,
        v_runtime.state_revision;
END
$$;

-- Admission is a row result, not an exception, so active calls can receive the
-- approved maintenance response and passive calls can stop silently.
DROP FUNCTION IF EXISTS rag_acquire_execution_lease(
    TEXT, TEXT, TEXT, TEXT, INTERVAL, JSONB
);
DROP FUNCTION IF EXISTS rag_acquire_execution_lease(
    TEXT, TEXT, TEXT, TEXT, INTERVAL, JSONB, TEXT
);
CREATE FUNCTION rag_acquire_execution_lease(
    p_collection_name TEXT,
    p_n8n_execution_id TEXT,
    p_transaction_id TEXT DEFAULT NULL,
    p_workflow_name TEXT DEFAULT NULL,
    p_lease_ttl INTERVAL DEFAULT interval '2 minutes',
    p_metadata JSONB DEFAULT '{}'::jsonb,
    p_maintenance_validation_run_id TEXT DEFAULT NULL
) RETURNS TABLE (
    lease_id UUID,
    acquired_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    runtime_revision BIGINT,
    newly_acquired BOOLEAN,
    admitted BOOLEAN,
    denial_reason TEXT,
    maintenance_validation BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_runtime rag_runtime_state%ROWTYPE;
    v_lease rag_active_execution_leases%ROWTYPE;
    v_validation BOOLEAN := false;
BEGIN
    IF coalesce(p_n8n_execution_id,'')=''
       OR p_lease_ttl <= interval '0 seconds'
       OR p_lease_ttl > interval '15 minutes'
       OR jsonb_typeof(p_metadata) <> 'object' THEN
        RAISE EXCEPTION 'invalid execution lease request' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_runtime FROM rag_runtime_state
    WHERE collection_name=p_collection_name FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown collection runtime' USING ERRCODE='P0002';
    END IF;
    v_validation := v_runtime.runtime_state='maintenance'
        AND v_runtime.active_incremental_run_id IS NOT NULL
        AND v_runtime.active_incremental_run_id=p_maintenance_validation_run_id;
    IF v_runtime.runtime_state <> 'serving' AND NOT v_validation THEN
        RETURN QUERY SELECT NULL::uuid, NULL::timestamptz, NULL::timestamptz,
            v_runtime.state_revision, false, false,
            'maintenance_in_progress'::text, false;
        RETURN;
    END IF;

    SELECT * INTO v_lease FROM rag_active_execution_leases
    WHERE collection_name=p_collection_name
      AND n8n_execution_id=p_n8n_execution_id FOR UPDATE;
    IF FOUND THEN
        IF v_lease.released_at IS NOT NULL
           OR v_lease.expires_at <= clock_timestamp()
           OR v_lease.transaction_id IS DISTINCT FROM p_transaction_id
           OR v_lease.workflow_name IS DISTINCT FROM p_workflow_name THEN
            RAISE EXCEPTION 'execution lease is closed or has different identity'
                USING ERRCODE='55000';
        END IF;
        RETURN QUERY SELECT v_lease.lease_id, v_lease.acquired_at,
            v_lease.expires_at, v_runtime.state_revision, false, true,
            NULL::text, v_validation;
        RETURN;
    END IF;
    INSERT INTO rag_active_execution_leases (
        collection_name,n8n_execution_id,transaction_id,workflow_name,
        expires_at,hard_expires_at,lease_metadata
    ) VALUES (
        p_collection_name,p_n8n_execution_id,p_transaction_id,p_workflow_name,
        clock_timestamp()+p_lease_ttl,
        clock_timestamp()+interval '15 minutes',
        p_metadata || jsonb_build_object('maintenance_validation',v_validation)
    ) RETURNING * INTO v_lease;
    RETURN QUERY SELECT v_lease.lease_id,v_lease.acquired_at,v_lease.expires_at,
        v_runtime.state_revision,true,true,NULL::text,v_validation;
END
$$;

-- Tighten the existing exit operation: success requires structural and
-- regression gates; failure after mutation requires completed rollback.
CREATE OR REPLACE FUNCTION rag_exit_incremental_maintenance(
    p_run_id TEXT,
    p_outcome TEXT,
    p_run_updates JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (
    incremental_run_id TEXT,
    run_state TEXT,
    runtime_state TEXT,
    runtime_revision BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
BEGIN
    IF p_outcome NOT IN ('completed','failed')
       OR jsonb_typeof(p_run_updates) <> 'object' THEN
        RAISE EXCEPTION 'outcome must be completed or failed with object updates'
            USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM rag_incremental_runs r
    WHERE r.incremental_run_id=p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %',p_run_id USING ERRCODE='P0002';
    END IF;
    SELECT * INTO v_runtime FROM rag_runtime_state
    WHERE collection_name=v_run.collection_name FOR UPDATE;
    IF v_run.run_state=p_outcome AND v_runtime.runtime_state='serving'
       AND v_runtime.active_incremental_run_id IS NULL THEN
        RETURN QUERY SELECT p_run_id,v_run.run_state,v_runtime.runtime_state,
            v_runtime.state_revision;
        RETURN;
    END IF;
    IF v_runtime.runtime_state <> 'maintenance'
       OR v_runtime.active_incremental_run_id <> p_run_id THEN
        RAISE EXCEPTION 'run does not own maintenance' USING ERRCODE='55000';
    END IF;
    IF p_outcome='completed' AND (
        v_run.run_state <> 'validating'
        OR v_run.structural_verification_result <> 'passed'
        OR v_run.regression_result <> 'passed'
    ) THEN
        RAISE EXCEPTION 'success requires structural and regression gates'
            USING ERRCODE='55000';
    END IF;
    IF p_outcome='failed'
       AND v_run.run_state IN ('replacing','rolling_back','validating')
       AND coalesce(v_run.rollback_status,'') <> 'completed' THEN
        RAISE EXCEPTION 'failed replacement cannot reopen before rollback'
            USING ERRCODE='55000';
    END IF;

    IF p_outcome='failed' THEN
        UPDATE rag_pending_chunk_work w SET status='pending',claimed_at=NULL,
            failure_reason=coalesce(w.failure_reason,'incremental_run_failed')
        WHERE w.status='claimed' AND w.capture_sequence <= v_run.batch_cutoff_sequence;
    END IF;
    UPDATE rag_incremental_runs r SET
        run_state=p_outcome,
        failure_step=coalesce(p_run_updates->>'failure_step',failure_step),
        failure_reason=coalesce(p_run_updates->>'failure_reason',failure_reason),
        run_metadata=run_metadata || p_run_updates,
        completed_at=now(),updated_at=now()
    WHERE r.incremental_run_id=p_run_id;
    UPDATE rag_runtime_state SET runtime_state='serving',
        active_incremental_run_id=NULL,state_revision=state_revision+1,
        changed_at=now()
    WHERE collection_name=v_run.collection_name RETURNING * INTO v_runtime;
    RETURN QUERY SELECT p_run_id,p_outcome,v_runtime.runtime_state,
        v_runtime.state_revision;
END
$$;
