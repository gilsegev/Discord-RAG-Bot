-- Phase 9C.3.5: durable incremental-run coordination and bounded execution
-- leases. n8n owns lifecycle decisions; Postgres only applies them atomically.

ALTER TABLE rag_chunk_replacement_plans
    ADD COLUMN IF NOT EXISTS source_corpus_version_id TEXT
        REFERENCES rag_corpus_versions(corpus_version_id),
    ADD COLUMN IF NOT EXISTS source_manifest_digest TEXT;

CREATE INDEX IF NOT EXISTS idx_rag_chunk_replacement_plans_source_corpus
    ON rag_chunk_replacement_plans(source_corpus_version_id);

CREATE TABLE IF NOT EXISTS rag_incremental_runs (
    incremental_run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE
        REFERENCES rag_chunk_replacement_plans(plan_id),
    collection_name TEXT NOT NULL,
    batch_cutoff_sequence BIGINT NOT NULL CHECK (batch_cutoff_sequence >= 0),
    corpus_version_before TEXT
        REFERENCES rag_corpus_versions(corpus_version_id),
    corpus_version_after TEXT
        REFERENCES rag_corpus_versions(corpus_version_id),
    run_state TEXT NOT NULL DEFAULT 'created' CHECK (
        run_state IN (
            'created', 'draining', 'maintenance', 'replacing', 'validating',
            'rolling_back', 'completed', 'review_needed', 'failed'
        )
    ),
    pending_message_count INTEGER NOT NULL DEFAULT 0
        CHECK (pending_message_count >= 0),
    claimed_message_count INTEGER NOT NULL DEFAULT 0
        CHECK (claimed_message_count >= 0),
    processed_message_count INTEGER NOT NULL DEFAULT 0
        CHECK (processed_message_count >= 0),
    deferred_message_count INTEGER NOT NULL DEFAULT 0
        CHECK (deferred_message_count >= 0),
    affected_group_count INTEGER NOT NULL DEFAULT 0
        CHECK (affected_group_count >= 0),
    old_point_count INTEGER NOT NULL DEFAULT 0 CHECK (old_point_count >= 0),
    replacement_point_count INTEGER NOT NULL DEFAULT 0
        CHECK (replacement_point_count >= 0),
    new_point_count INTEGER NOT NULL DEFAULT 0 CHECK (new_point_count >= 0),
    reused_point_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_point_count >= 0),
    deleted_point_count INTEGER NOT NULL DEFAULT 0
        CHECK (deleted_point_count >= 0),
    snapshot_bytes BIGINT CHECK (snapshot_bytes >= 0),
    snapshot_digest TEXT,
    snapshot_uri TEXT,
    snapshot_retained_until TIMESTAMPTZ,
    phase_durations JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(phase_durations) = 'object'),
    regression_run_id TEXT,
    regression_result TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    failure_step TEXT,
    failure_reason TEXT,
    rollback_status TEXT,
    rollback_reason TEXT,
    run_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(run_metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_runtime_state (
    collection_name TEXT PRIMARY KEY,
    runtime_state TEXT NOT NULL DEFAULT 'serving' CHECK (
        runtime_state IN ('serving', 'draining', 'maintenance')
    ),
    active_incremental_run_id TEXT
        REFERENCES rag_incremental_runs(incremental_run_id),
    state_revision BIGINT NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (runtime_state = 'serving' AND active_incremental_run_id IS NULL)
        OR
        (runtime_state <> 'serving' AND active_incremental_run_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS rag_active_execution_leases (
    lease_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_name TEXT NOT NULL
        REFERENCES rag_runtime_state(collection_name),
    n8n_execution_id TEXT NOT NULL,
    transaction_id TEXT,
    workflow_name TEXT,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    hard_expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    lease_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(lease_metadata) = 'object'),
    CHECK (expires_at > acquired_at),
    CHECK (expires_at <= hard_expires_at),
    CHECK (hard_expires_at > acquired_at),
    CHECK (released_at IS NULL OR released_at >= acquired_at),
    UNIQUE (collection_name, n8n_execution_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_incremental_runs_collection_state
    ON rag_incremental_runs(collection_name, run_state, created_at);
CREATE INDEX IF NOT EXISTS idx_rag_active_execution_leases_live
    ON rag_active_execution_leases(collection_name, expires_at)
    WHERE released_at IS NULL;

INSERT INTO rag_runtime_state (collection_name)
SELECT collection_name
FROM (
    SELECT 'tpm_unite_history'::text AS collection_name
    UNION
    SELECT collection_name FROM rag_corpus_versions
    UNION
    SELECT collection_name FROM rag_chunk_replacement_plans
) known_collections
ON CONFLICT (collection_name) DO NOTHING;

CREATE OR REPLACE FUNCTION rag_count_live_execution_leases(
    p_collection_name TEXT
) RETURNS BIGINT
LANGUAGE sql
VOLATILE
AS $$
    SELECT count(*)
    FROM rag_active_execution_leases l
    WHERE l.collection_name = p_collection_name
      AND l.released_at IS NULL
      AND l.expires_at > clock_timestamp()
$$;

CREATE OR REPLACE FUNCTION rag_create_incremental_run(
    p_run_id TEXT,
    p_plan_id TEXT,
    p_collection_name TEXT,
    p_run_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (
    incremental_run_id TEXT,
    run_state TEXT,
    runtime_state TEXT,
    runtime_revision BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_plan rag_chunk_replacement_plans%ROWTYPE;
    v_source_corpus rag_corpus_versions%ROWTYPE;
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
BEGIN
    IF coalesce(p_run_id, '') = '' OR jsonb_typeof(p_run_metadata) <> 'object' THEN
        RAISE EXCEPTION 'run ID and object metadata are required'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_run.plan_id <> p_plan_id
           OR v_run.collection_name <> p_collection_name THEN
            RAISE EXCEPTION 'run ID already belongs to another plan or collection'
                USING ERRCODE = '23505';
        END IF;
        SELECT * INTO v_runtime
        FROM rag_runtime_state rs
        WHERE rs.collection_name = p_collection_name
        FOR UPDATE;
        RETURN QUERY SELECT v_run.incremental_run_id, v_run.run_state,
            v_runtime.runtime_state, v_runtime.state_revision;
        RETURN;
    END IF;

    SELECT * INTO v_plan
    FROM rag_chunk_replacement_plans p
    WHERE p.plan_id = p_plan_id;
    IF NOT FOUND OR v_plan.collection_name <> p_collection_name THEN
        RAISE EXCEPTION 'unknown plan or collection mismatch'
            USING ERRCODE = '22023';
    END IF;
    IF v_plan.status <> 'shadow_validated'
       OR v_plan.source_corpus_version_id IS NULL
       OR v_plan.source_manifest_digest IS NULL THEN
        RAISE EXCEPTION 'plan lacks complete shadow-validated source evidence'
            USING ERRCODE = '55000';
    END IF;
    SELECT * INTO v_source_corpus
    FROM rag_corpus_versions cv
    WHERE cv.corpus_version_id = v_plan.source_corpus_version_id
      AND cv.collection_name = p_collection_name
      AND cv.status = 'healthy';
    IF NOT FOUND
       OR v_source_corpus.manifest_digest <> v_plan.source_manifest_digest THEN
        RAISE EXCEPTION 'plan source is not the current healthy corpus'
            USING ERRCODE = '55000';
    END IF;

    INSERT INTO rag_runtime_state (collection_name)
    VALUES (p_collection_name)
    ON CONFLICT (collection_name) DO NOTHING;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = p_collection_name
    FOR UPDATE;

    INSERT INTO rag_incremental_runs (
        incremental_run_id, plan_id, collection_name, batch_cutoff_sequence,
        corpus_version_before, pending_message_count, old_point_count,
        replacement_point_count, affected_group_count, run_metadata
    ) VALUES (
        p_run_id, p_plan_id, p_collection_name, v_plan.batch_cutoff_sequence,
        v_plan.source_corpus_version_id, v_plan.pending_message_count,
        v_plan.old_point_count, v_plan.replacement_point_count,
        (SELECT count(*)::integer
         FROM rag_chunk_replacement_plan_groups g
         WHERE g.plan_id = p_plan_id),
        p_run_metadata
    )
    ON CONFLICT ON CONSTRAINT rag_incremental_runs_pkey DO NOTHING;

    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF v_run.plan_id <> p_plan_id
       OR v_run.collection_name <> p_collection_name THEN
        RAISE EXCEPTION 'run ID already belongs to another plan or collection'
            USING ERRCODE = '23505';
    END IF;

    RETURN QUERY SELECT v_run.incremental_run_id, v_run.run_state,
        v_runtime.runtime_state, v_runtime.state_revision;
END
$$;

CREATE OR REPLACE FUNCTION rag_update_incremental_run(
    p_run_id TEXT,
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
    IF jsonb_typeof(p_run_updates) <> 'object' THEN
        RAISE EXCEPTION 'run updates must be a JSON object'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %', p_run_id
            USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = v_run.collection_name
    FOR UPDATE;
    IF v_runtime.runtime_state <> 'serving'
       AND v_runtime.active_incremental_run_id <> p_run_id THEN
        RAISE EXCEPTION 'run does not own collection runtime'
            USING ERRCODE = '55000';
    END IF;

    UPDATE rag_incremental_runs r
    SET claimed_message_count = coalesce(
            (p_run_updates->>'claimed_message_count')::integer,
            r.claimed_message_count
        ),
        processed_message_count = coalesce(
            (p_run_updates->>'processed_message_count')::integer,
            r.processed_message_count
        ),
        deferred_message_count = coalesce(
            (p_run_updates->>'deferred_message_count')::integer,
            r.deferred_message_count
        ),
        retry_count = coalesce(
            (p_run_updates->>'retry_count')::integer, r.retry_count
        ),
        failure_step = coalesce(p_run_updates->>'failure_step', r.failure_step),
        failure_reason = coalesce(p_run_updates->>'failure_reason', r.failure_reason),
        run_metadata = r.run_metadata || p_run_updates,
        updated_at = now()
    WHERE r.incremental_run_id = p_run_id
    RETURNING r.* INTO v_run;

    RETURN QUERY SELECT v_run.incremental_run_id, v_run.run_state,
        v_runtime.runtime_state, v_runtime.state_revision;
END
$$;

CREATE OR REPLACE FUNCTION rag_fail_incremental_run(
    p_run_id TEXT,
    p_failure_step TEXT,
    p_failure_reason TEXT,
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
    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF NOT FOUND OR v_run.run_state <> 'created' THEN
        RAISE EXCEPTION 'only a created simulation run can fail while serving'
            USING ERRCODE = '55000';
    END IF;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = v_run.collection_name
    FOR UPDATE;
    IF v_runtime.runtime_state <> 'serving'
       OR v_runtime.active_incremental_run_id IS NOT NULL THEN
        RAISE EXCEPTION 'collection is not in unowned serving state'
            USING ERRCODE = '55000';
    END IF;
    UPDATE rag_incremental_runs r
    SET run_state = 'failed',
        failure_step = p_failure_step,
        failure_reason = p_failure_reason,
        run_metadata = r.run_metadata || p_run_updates,
        completed_at = now(),
        updated_at = now()
    WHERE r.incremental_run_id = p_run_id
    RETURNING r.* INTO v_run;
    RETURN QUERY SELECT v_run.incremental_run_id, v_run.run_state,
        v_runtime.runtime_state, v_runtime.state_revision;
END
$$;

CREATE OR REPLACE FUNCTION rag_begin_incremental_drain(
    p_run_id TEXT,
    p_expected_runtime_revision BIGINT
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
    v_plan rag_chunk_replacement_plans%ROWTYPE;
    v_corpus rag_corpus_versions%ROWTYPE;
BEGIN
    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %', p_run_id
            USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = v_run.collection_name
    FOR UPDATE;
    IF v_run.run_state = 'draining'
       AND v_runtime.runtime_state = 'draining'
       AND v_runtime.active_incremental_run_id = p_run_id THEN
        RETURN QUERY SELECT p_run_id, v_run.run_state,
            v_runtime.runtime_state, v_runtime.state_revision;
        RETURN;
    END IF;
    IF v_run.run_state <> 'created' THEN
        RAISE EXCEPTION 'drain requires a created run'
            USING ERRCODE = '55000';
    END IF;
    SELECT * INTO v_plan
    FROM rag_chunk_replacement_plans p
    WHERE p.plan_id = v_run.plan_id;
    SELECT * INTO v_corpus
    FROM rag_corpus_versions cv
    WHERE cv.corpus_version_id = v_plan.source_corpus_version_id
      AND cv.collection_name = v_run.collection_name
      AND cv.status = 'healthy';
    IF v_plan.status <> 'shadow_validated'
       OR NOT FOUND
       OR v_corpus.manifest_digest <> v_plan.source_manifest_digest THEN
        RAISE EXCEPTION 'plan source is no longer the healthy corpus'
            USING ERRCODE = '55000';
    END IF;
    IF v_runtime.runtime_state <> 'serving'
       OR v_runtime.active_incremental_run_id IS NOT NULL
       OR v_runtime.state_revision <> p_expected_runtime_revision THEN
        RAISE EXCEPTION 'runtime is not available at expected revision'
            USING ERRCODE = '40001';
    END IF;

    UPDATE rag_incremental_runs r
    SET run_state = 'draining',
        started_at = coalesce(r.started_at, now()),
        updated_at = now()
    WHERE r.incremental_run_id = p_run_id;
    UPDATE rag_runtime_state rs
    SET runtime_state = 'draining',
        active_incremental_run_id = p_run_id,
        state_revision = rs.state_revision + 1,
        changed_at = now()
    WHERE rs.collection_name = v_run.collection_name
    RETURNING rs.* INTO v_runtime;
    RETURN QUERY SELECT p_run_id, 'draining'::text,
        v_runtime.runtime_state, v_runtime.state_revision;
END
$$;

CREATE OR REPLACE FUNCTION rag_enter_incremental_maintenance(
    p_run_id TEXT,
    p_expected_runtime_revision BIGINT
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
    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %', p_run_id
            USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = v_run.collection_name
    FOR UPDATE;
    IF v_run.run_state = 'maintenance'
       AND v_runtime.runtime_state = 'maintenance'
       AND v_runtime.active_incremental_run_id = p_run_id THEN
        RETURN QUERY SELECT p_run_id, v_run.run_state,
            v_runtime.runtime_state, v_runtime.state_revision;
        RETURN;
    END IF;
    IF v_run.run_state <> 'draining' THEN
        RAISE EXCEPTION 'maintenance requires a draining run'
            USING ERRCODE = '55000';
    END IF;
    IF v_runtime.runtime_state <> 'draining'
       OR v_runtime.active_incremental_run_id <> p_run_id
       OR v_runtime.state_revision <> p_expected_runtime_revision THEN
        RAISE EXCEPTION 'run does not own draining at expected revision'
            USING ERRCODE = '40001';
    END IF;
    IF rag_count_live_execution_leases(v_run.collection_name) <> 0 THEN
        RAISE EXCEPTION 'collection still has live execution leases'
            USING ERRCODE = '55000';
    END IF;

    UPDATE rag_incremental_runs r
    SET run_state = 'maintenance',
        started_at = coalesce(r.started_at, now()),
        updated_at = now()
    WHERE r.incremental_run_id = p_run_id;
    UPDATE rag_runtime_state rs
    SET runtime_state = 'maintenance',
        active_incremental_run_id = p_run_id,
        state_revision = rs.state_revision + 1,
        changed_at = now()
    WHERE rs.collection_name = v_run.collection_name
    RETURNING rs.* INTO v_runtime;
    RETURN QUERY SELECT p_run_id, 'maintenance'::text,
        v_runtime.runtime_state, v_runtime.state_revision;
END
$$;

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
    IF p_outcome NOT IN ('completed', 'failed')
       OR jsonb_typeof(p_run_updates) <> 'object' THEN
        RAISE EXCEPTION 'outcome must be completed or failed with object updates'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown incremental run %', p_run_id
            USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = v_run.collection_name
    FOR UPDATE;
    IF v_run.run_state = p_outcome
       AND v_runtime.runtime_state = 'serving'
       AND v_runtime.active_incremental_run_id IS NULL THEN
        RETURN QUERY SELECT p_run_id, v_run.run_state,
            v_runtime.runtime_state, v_runtime.state_revision;
        RETURN;
    END IF;
    IF v_run.run_state <> 'maintenance'
       OR v_runtime.runtime_state <> 'maintenance'
       OR v_runtime.active_incremental_run_id <> p_run_id THEN
        RAISE EXCEPTION 'run does not own maintenance'
            USING ERRCODE = '55000';
    END IF;

    UPDATE rag_incremental_runs r
    SET run_state = p_outcome,
        failure_step = coalesce(p_run_updates->>'failure_step', r.failure_step),
        failure_reason = coalesce(p_run_updates->>'failure_reason', r.failure_reason),
        run_metadata = r.run_metadata || p_run_updates,
        completed_at = now(),
        updated_at = now()
    WHERE r.incremental_run_id = p_run_id;
    UPDATE rag_runtime_state rs
    SET runtime_state = 'serving',
        active_incremental_run_id = NULL,
        state_revision = rs.state_revision + 1,
        changed_at = now()
    WHERE rs.collection_name = v_run.collection_name
    RETURNING rs.* INTO v_runtime;
    RETURN QUERY SELECT p_run_id, p_outcome, v_runtime.runtime_state,
        v_runtime.state_revision;
END
$$;

CREATE OR REPLACE FUNCTION rag_acquire_execution_lease(
    p_collection_name TEXT,
    p_n8n_execution_id TEXT,
    p_transaction_id TEXT DEFAULT NULL,
    p_workflow_name TEXT DEFAULT NULL,
    p_lease_ttl INTERVAL DEFAULT interval '2 minutes',
    p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (
    lease_id UUID,
    acquired_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    runtime_revision BIGINT,
    newly_acquired BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_runtime rag_runtime_state%ROWTYPE;
    v_lease rag_active_execution_leases%ROWTYPE;
BEGIN
    IF coalesce(p_n8n_execution_id, '') = ''
       OR p_lease_ttl <= interval '0 seconds'
       OR p_lease_ttl > interval '15 minutes'
       OR jsonb_typeof(p_metadata) <> 'object' THEN
        RAISE EXCEPTION 'invalid execution lease request'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = p_collection_name
    FOR UPDATE;
    IF NOT FOUND OR v_runtime.runtime_state <> 'serving' THEN
        RAISE EXCEPTION 'collection is not serving; lease denied'
            USING ERRCODE = '55000';
    END IF;
    SELECT * INTO v_lease
    FROM rag_active_execution_leases l
    WHERE l.collection_name = p_collection_name
      AND l.n8n_execution_id = p_n8n_execution_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_lease.released_at IS NOT NULL
           OR v_lease.expires_at <= clock_timestamp()
           OR v_lease.transaction_id IS DISTINCT FROM p_transaction_id
           OR v_lease.workflow_name IS DISTINCT FROM p_workflow_name THEN
            RAISE EXCEPTION 'execution lease is closed or has different identity'
                USING ERRCODE = '55000';
        END IF;
        RETURN QUERY SELECT v_lease.lease_id, v_lease.acquired_at,
            v_lease.expires_at, v_runtime.state_revision, false;
        RETURN;
    END IF;
    INSERT INTO rag_active_execution_leases (
        collection_name, n8n_execution_id, transaction_id, workflow_name,
        expires_at, hard_expires_at, lease_metadata
    ) VALUES (
        p_collection_name, p_n8n_execution_id, p_transaction_id,
        p_workflow_name, clock_timestamp() + p_lease_ttl,
        clock_timestamp() + interval '15 minutes', p_metadata
    )
    RETURNING * INTO v_lease;
    RETURN QUERY SELECT v_lease.lease_id, v_lease.acquired_at,
        v_lease.expires_at, v_runtime.state_revision, true;
END
$$;

CREATE OR REPLACE FUNCTION rag_heartbeat_execution_lease(
    p_lease_id UUID,
    p_lease_ttl INTERVAL DEFAULT interval '2 minutes'
) RETURNS TABLE (lease_id UUID, expires_at TIMESTAMPTZ)
LANGUAGE plpgsql
AS $$
DECLARE
    v_lease rag_active_execution_leases%ROWTYPE;
BEGIN
    IF p_lease_ttl <= interval '0 seconds'
       OR p_lease_ttl > interval '15 minutes' THEN
        RAISE EXCEPTION 'invalid lease TTL' USING ERRCODE = '22023';
    END IF;
    UPDATE rag_active_execution_leases l
    SET heartbeat_at = clock_timestamp(),
        expires_at = least(clock_timestamp() + p_lease_ttl, l.hard_expires_at)
    WHERE l.lease_id = p_lease_id
      AND l.released_at IS NULL
      AND l.expires_at > clock_timestamp()
    RETURNING l.* INTO v_lease;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease is missing, released, or expired'
            USING ERRCODE = '55000';
    END IF;
    RETURN QUERY SELECT v_lease.lease_id, v_lease.expires_at;
END
$$;

CREATE OR REPLACE FUNCTION rag_release_execution_lease(
    p_lease_id UUID
) RETURNS TABLE (lease_id UUID, released_at TIMESTAMPTZ)
LANGUAGE plpgsql
AS $$
DECLARE
    v_lease rag_active_execution_leases%ROWTYPE;
BEGIN
    UPDATE rag_active_execution_leases l
    SET released_at = coalesce(l.released_at, clock_timestamp())
    WHERE l.lease_id = p_lease_id
    RETURNING l.* INTO v_lease;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown lease %', p_lease_id USING ERRCODE = 'P0002';
    END IF;
    RETURN QUERY SELECT v_lease.lease_id, v_lease.released_at;
END
$$;
