-- Phase 9C.3.5: durable incremental-run lifecycle, audit events, and
-- race-safe execution leases. Postgres enforces transitions atomically; n8n
-- remains the owner of lifecycle decisions.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Phase 9C.4 needs terminal plan states and a durable link to the corpus
-- version against which a plan was computed. Existing plans remain nullable
-- because their source version was not recorded by Phase 9C.3.
ALTER TABLE rag_chunk_replacement_plans
    ADD COLUMN IF NOT EXISTS source_corpus_version_id TEXT
        REFERENCES rag_corpus_versions(corpus_version_id),
    ADD COLUMN IF NOT EXISTS source_manifest_digest TEXT;

DO $$
BEGIN
    ALTER TABLE rag_chunk_replacement_plans
        DROP CONSTRAINT IF EXISTS rag_chunk_replacement_plans_status_check;
    ALTER TABLE rag_chunk_replacement_plans
        ADD CONSTRAINT rag_chunk_replacement_plans_status_check CHECK (
            status IN (
                'planned', 'shadow_validated', 'deferred', 'failed',
                'invalidated', 'applied'
            )
        );
END
$$;

CREATE INDEX IF NOT EXISTS idx_rag_chunk_replacement_plans_source_corpus
    ON rag_chunk_replacement_plans(source_corpus_version_id);

CREATE TABLE IF NOT EXISTS rag_incremental_runs (
    incremental_run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL
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
    -- Regression execution currently has no durable parent table, so this is
    -- the external n8n regression run identifier rather than a foreign key.
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (plan_id)
);

CREATE TABLE IF NOT EXISTS rag_runtime_state (
    collection_name TEXT PRIMARY KEY,
    runtime_state TEXT NOT NULL DEFAULT 'serving' CHECK (
        runtime_state IN (
            'serving', 'draining', 'maintenance', 'validating',
            'review_needed', 'rolling_back'
        )
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

CREATE TABLE IF NOT EXISTS rag_incremental_run_events (
    event_id BIGSERIAL PRIMARY KEY,
    incremental_run_id TEXT NOT NULL
        REFERENCES rag_incremental_runs(incremental_run_id),
    idempotency_key TEXT NOT NULL,
    event_name TEXT NOT NULL,
    previous_run_state TEXT,
    new_run_state TEXT NOT NULL,
    previous_runtime_state TEXT NOT NULL,
    new_runtime_state TEXT NOT NULL,
    runtime_revision BIGINT NOT NULL CHECK (runtime_revision >= 0),
    n8n_execution_id TEXT,
    event_payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(event_payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (incremental_run_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_rag_incremental_runs_collection_state
    ON rag_incremental_runs(collection_name, run_state, created_at);
CREATE INDEX IF NOT EXISTS idx_rag_incremental_run_events_run_created
    ON rag_incremental_run_events(incremental_run_id, created_at, event_id);

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

CREATE INDEX IF NOT EXISTS idx_rag_active_execution_leases_live
    ON rag_active_execution_leases(collection_name, expires_at)
    WHERE released_at IS NULL;

-- Seed every known collection and the production default. ON CONFLICT keeps
-- this migration safe to rerun without resetting live runtime state.
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

CREATE OR REPLACE FUNCTION rag_prevent_incremental_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'rag_incremental_run_events is append-only'
        USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS rag_incremental_run_events_append_only
    ON rag_incremental_run_events;
CREATE TRIGGER rag_incremental_run_events_append_only
    BEFORE UPDATE OR DELETE ON rag_incremental_run_events
    FOR EACH ROW EXECUTE FUNCTION rag_prevent_incremental_event_mutation();

CREATE OR REPLACE FUNCTION rag_valid_run_transition(
    p_from TEXT,
    p_to TEXT
) RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_from = p_to OR (p_from, p_to) IN (
        ('created', 'draining'),
        ('created', 'failed'),
        ('draining', 'maintenance'),
        ('draining', 'failed'),
        ('maintenance', 'replacing'),
        ('maintenance', 'validating'),
        ('maintenance', 'rolling_back'),
        ('maintenance', 'review_needed'),
        ('maintenance', 'failed'),
        ('replacing', 'validating'),
        ('replacing', 'rolling_back'),
        ('replacing', 'review_needed'),
        ('replacing', 'failed'),
        ('validating', 'completed'),
        ('validating', 'rolling_back'),
        ('validating', 'review_needed'),
        ('validating', 'failed'),
        ('rolling_back', 'completed'),
        ('rolling_back', 'review_needed'),
        ('rolling_back', 'failed'),
        ('review_needed', 'rolling_back'),
        ('review_needed', 'failed'),
        ('failed', 'rolling_back')
    )
$$;

CREATE OR REPLACE FUNCTION rag_valid_runtime_transition(
    p_from TEXT,
    p_to TEXT
) RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_from = p_to OR (p_from, p_to) IN (
        ('serving', 'draining'),
        ('draining', 'maintenance'),
        ('draining', 'serving'),
        ('maintenance', 'validating'),
        ('maintenance', 'rolling_back'),
        ('maintenance', 'review_needed'),
        ('validating', 'serving'),
        ('validating', 'rolling_back'),
        ('validating', 'review_needed'),
        ('rolling_back', 'serving'),
        ('rolling_back', 'review_needed'),
        ('review_needed', 'rolling_back')
    )
$$;

CREATE OR REPLACE FUNCTION rag_valid_lifecycle_pair(
    p_run_state TEXT,
    p_runtime_state TEXT
) RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT (p_run_state, p_runtime_state) IN (
        ('created', 'serving'),
        ('draining', 'draining'),
        ('maintenance', 'maintenance'),
        ('replacing', 'maintenance'),
        ('validating', 'validating'),
        ('rolling_back', 'rolling_back'),
        ('review_needed', 'review_needed'),
        ('completed', 'serving'),
        ('failed', 'serving'),
        ('failed', 'review_needed')
    )
$$;

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
    p_event_idempotency_key TEXT,
    p_n8n_execution_id TEXT DEFAULT NULL,
    p_event_payload JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (
    incremental_run_id TEXT,
    run_state TEXT,
    runtime_state TEXT,
    runtime_revision BIGINT,
    event_id BIGINT,
    event_created BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_plan rag_chunk_replacement_plans%ROWTYPE;
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_event rag_incremental_run_events%ROWTYPE;
    v_source_corpus rag_corpus_versions%ROWTYPE;
    v_inserted BOOLEAN := false;
BEGIN
    IF coalesce(p_run_id, '') = '' OR coalesce(p_event_idempotency_key, '') = '' THEN
        RAISE EXCEPTION 'run ID and idempotency key are required'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_event_payload) <> 'object' THEN
        RAISE EXCEPTION 'event payload must be a JSON object'
            USING ERRCODE = '22023';
    END IF;

    -- Return a previously committed create before revalidating mutable plan
    -- state. For example, a later retry must remain idempotent after Phase
    -- 9C.4 changes the plan from shadow_validated to applied.
    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_run.plan_id <> p_plan_id
           OR v_run.collection_name <> p_collection_name THEN
            RAISE EXCEPTION 'run % already exists with different immutable identity',
                p_run_id USING ERRCODE = '23505';
        END IF;
        SELECT * INTO v_event
        FROM rag_incremental_run_events e
        WHERE e.incremental_run_id = p_run_id
          AND e.idempotency_key = p_event_idempotency_key;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'run % was already created with another idempotency key',
                p_run_id USING ERRCODE = '23505';
        END IF;
        IF v_event.n8n_execution_id IS DISTINCT FROM p_n8n_execution_id
           OR v_event.event_payload <> p_event_payload THEN
            RAISE EXCEPTION 'idempotency key % was reused with different create data',
                p_event_idempotency_key USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT v_run.incremental_run_id, v_event.new_run_state,
            v_event.new_runtime_state, v_event.runtime_revision,
            v_event.event_id, false;
        RETURN;
    END IF;

    SELECT * INTO v_plan
    FROM rag_chunk_replacement_plans p
    WHERE p.plan_id = p_plan_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown replacement plan %', p_plan_id
            USING ERRCODE = '23503';
    END IF;
    IF v_plan.collection_name <> p_collection_name THEN
        RAISE EXCEPTION 'plan % belongs to collection %, not %',
            p_plan_id, v_plan.collection_name, p_collection_name
            USING ERRCODE = '22023';
    END IF;
    IF v_plan.status <> 'shadow_validated'
       OR v_plan.source_corpus_version_id IS NULL
       OR v_plan.source_manifest_digest IS NULL THEN
        RAISE EXCEPTION 'plan % lacks complete shadow-validated source corpus evidence',
            p_plan_id USING ERRCODE = '55000';
    END IF;
    SELECT * INTO v_source_corpus
    FROM rag_corpus_versions cv
    WHERE cv.corpus_version_id = v_plan.source_corpus_version_id
      AND cv.collection_name = p_collection_name
      AND cv.status = 'healthy';
    IF NOT FOUND
       OR v_source_corpus.manifest_digest <> v_plan.source_manifest_digest THEN
        RAISE EXCEPTION 'plan % source corpus is not the current healthy corpus',
            p_plan_id USING ERRCODE = '55000';
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
        p_event_payload
    )
    ON CONFLICT ON CONSTRAINT rag_incremental_runs_pkey DO NOTHING;

    SELECT * INTO v_run
    FROM rag_incremental_runs r
    WHERE r.incremental_run_id = p_run_id
    FOR UPDATE;

    IF v_run.plan_id <> p_plan_id OR v_run.collection_name <> p_collection_name THEN
        RAISE EXCEPTION 'run % already exists with different immutable identity',
            p_run_id USING ERRCODE = '23505';
    END IF;

    SELECT * INTO v_event
    FROM rag_incremental_run_events e
    WHERE e.incremental_run_id = p_run_id
      AND e.idempotency_key = p_event_idempotency_key;

    IF FOUND THEN
        IF v_event.n8n_execution_id IS DISTINCT FROM p_n8n_execution_id
           OR v_event.event_payload <> p_event_payload THEN
            RAISE EXCEPTION 'idempotency key % was reused with different create data',
                p_event_idempotency_key USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT v_run.incremental_run_id, v_event.new_run_state,
            v_event.new_runtime_state, v_event.runtime_revision,
            v_event.event_id, false;
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM rag_incremental_run_events e
        WHERE e.incremental_run_id = p_run_id
    ) THEN
        RAISE EXCEPTION 'run % was already created with another idempotency key',
            p_run_id USING ERRCODE = '23505';
    END IF;

    INSERT INTO rag_incremental_run_events (
        incremental_run_id, idempotency_key, event_name,
        previous_run_state, new_run_state,
        previous_runtime_state, new_runtime_state, runtime_revision,
        n8n_execution_id, event_payload
    ) VALUES (
        p_run_id, p_event_idempotency_key, 'incremental.run_created',
        NULL, 'created', v_runtime.runtime_state, v_runtime.runtime_state,
        v_runtime.state_revision, p_n8n_execution_id, p_event_payload
    )
    RETURNING * INTO v_event;
    v_inserted := true;

    RETURN QUERY SELECT v_run.incremental_run_id, v_run.run_state,
        v_runtime.runtime_state, v_runtime.state_revision, v_event.event_id,
        v_inserted;
END
$$;

CREATE OR REPLACE FUNCTION rag_transition_incremental_run(
    p_run_id TEXT,
    p_event_idempotency_key TEXT,
    p_event_name TEXT,
    p_expected_run_state TEXT,
    p_new_run_state TEXT,
    p_expected_runtime_state TEXT,
    p_new_runtime_state TEXT,
    p_expected_runtime_revision BIGINT,
    p_n8n_execution_id TEXT DEFAULT NULL,
    p_event_payload JSONB DEFAULT '{}'::jsonb
) RETURNS TABLE (
    incremental_run_id TEXT,
    run_state TEXT,
    runtime_state TEXT,
    runtime_revision BIGINT,
    event_id BIGINT,
    event_created BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_run rag_incremental_runs%ROWTYPE;
    v_runtime rag_runtime_state%ROWTYPE;
    v_event rag_incremental_run_events%ROWTYPE;
    v_new_revision BIGINT;
BEGIN
    IF coalesce(p_event_idempotency_key, '') = ''
       OR coalesce(p_event_name, '') = '' THEN
        RAISE EXCEPTION 'idempotency key and event name are required'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_event_payload) <> 'object' THEN
        RAISE EXCEPTION 'event payload must be a JSON object'
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

    -- Idempotency is checked before expected-state validation so a retried
    -- delivery returns the original committed result.
    SELECT * INTO v_event
    FROM rag_incremental_run_events e
    WHERE e.incremental_run_id = p_run_id
      AND e.idempotency_key = p_event_idempotency_key;
    IF FOUND THEN
        IF v_event.event_name <> p_event_name
           OR v_event.previous_run_state IS DISTINCT FROM p_expected_run_state
           OR v_event.new_run_state <> p_new_run_state
           OR v_event.previous_runtime_state <> p_expected_runtime_state
           OR v_event.new_runtime_state <> p_new_runtime_state
           OR v_event.n8n_execution_id IS DISTINCT FROM p_n8n_execution_id
           OR v_event.event_payload <> p_event_payload THEN
            RAISE EXCEPTION 'idempotency key % was reused with different transition data',
                p_event_idempotency_key USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT p_run_id, v_event.new_run_state,
            v_event.new_runtime_state, v_event.runtime_revision,
            v_event.event_id, false;
        RETURN;
    END IF;

    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = v_run.collection_name
    FOR UPDATE;

    IF v_run.run_state <> p_expected_run_state THEN
        RAISE EXCEPTION 'run state mismatch: expected %, found %',
            p_expected_run_state, v_run.run_state USING ERRCODE = '40001';
    END IF;
    IF v_runtime.runtime_state <> p_expected_runtime_state
       OR v_runtime.state_revision <> p_expected_runtime_revision THEN
        RAISE EXCEPTION 'runtime state/revision mismatch: expected %/%, found %/%',
            p_expected_runtime_state, p_expected_runtime_revision,
            v_runtime.runtime_state, v_runtime.state_revision
            USING ERRCODE = '40001';
    END IF;
    IF NOT rag_valid_lifecycle_pair(
        p_expected_run_state, p_expected_runtime_state
    ) THEN
        RAISE EXCEPTION 'invalid current run/runtime state pair %/%',
            p_expected_run_state, p_expected_runtime_state
            USING ERRCODE = '55000';
    END IF;
    IF NOT rag_valid_run_transition(p_expected_run_state, p_new_run_state) THEN
        RAISE EXCEPTION 'invalid run transition % -> %',
            p_expected_run_state, p_new_run_state USING ERRCODE = '22023';
    END IF;
    IF NOT rag_valid_runtime_transition(
        p_expected_runtime_state, p_new_runtime_state
    ) THEN
        RAISE EXCEPTION 'invalid runtime transition % -> %',
            p_expected_runtime_state, p_new_runtime_state
            USING ERRCODE = '22023';
    END IF;
    IF NOT rag_valid_lifecycle_pair(p_new_run_state, p_new_runtime_state) THEN
        RAISE EXCEPTION 'invalid combined run/runtime target state %/%',
            p_new_run_state, p_new_runtime_state USING ERRCODE = '22023';
    END IF;
    IF p_expected_runtime_state = 'draining'
       AND p_new_runtime_state = 'maintenance'
       AND rag_count_live_execution_leases(v_run.collection_name) <> 0 THEN
        RAISE EXCEPTION 'collection % still has live execution leases',
            v_run.collection_name USING ERRCODE = '55000';
    END IF;
    IF p_expected_runtime_state = 'serving'
       AND p_new_runtime_state = 'draining'
       AND EXISTS (
           SELECT 1 FROM rag_runtime_state occupied
           WHERE occupied.collection_name = v_run.collection_name
             AND occupied.active_incremental_run_id IS NOT NULL
             AND occupied.active_incremental_run_id <> p_run_id
       ) THEN
        RAISE EXCEPTION 'collection % already has an active incremental run',
            v_run.collection_name USING ERRCODE = '55000';
    END IF;

    v_new_revision := v_runtime.state_revision
        + CASE WHEN p_new_runtime_state <> p_expected_runtime_state THEN 1 ELSE 0 END;

    UPDATE rag_incremental_runs r
    SET run_state = p_new_run_state,
        started_at = CASE
            WHEN p_new_run_state = 'draining' THEN coalesce(r.started_at, now())
            ELSE r.started_at
        END,
        completed_at = CASE
            WHEN p_new_run_state IN ('completed', 'failed') THEN now()
            ELSE r.completed_at
        END,
        updated_at = now()
    WHERE r.incremental_run_id = p_run_id;

    UPDATE rag_runtime_state rs
    SET runtime_state = p_new_runtime_state,
        active_incremental_run_id = CASE
            WHEN p_new_runtime_state = 'serving' THEN NULL
            ELSE p_run_id
        END,
        state_revision = v_new_revision,
        changed_at = CASE
            WHEN p_new_runtime_state <> p_expected_runtime_state THEN now()
            ELSE rs.changed_at
        END
    WHERE rs.collection_name = v_run.collection_name;

    INSERT INTO rag_incremental_run_events (
        incremental_run_id, idempotency_key, event_name,
        previous_run_state, new_run_state,
        previous_runtime_state, new_runtime_state, runtime_revision,
        n8n_execution_id, event_payload
    ) VALUES (
        p_run_id, p_event_idempotency_key, p_event_name,
        p_expected_run_state, p_new_run_state,
        p_expected_runtime_state, p_new_runtime_state, v_new_revision,
        p_n8n_execution_id, p_event_payload
    )
    RETURNING * INTO v_event;

    RETURN QUERY SELECT p_run_id, p_new_run_state, p_new_runtime_state,
        v_new_revision, v_event.event_id, true;
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
    IF coalesce(p_n8n_execution_id, '') = '' THEN
        RAISE EXCEPTION 'n8n execution ID is required' USING ERRCODE = '22023';
    END IF;
    IF p_lease_ttl <= interval '0 seconds'
       OR p_lease_ttl > interval '15 minutes' THEN
        RAISE EXCEPTION 'lease TTL must be greater than zero and at most 15 minutes'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_metadata) <> 'object' THEN
        RAISE EXCEPTION 'lease metadata must be a JSON object'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_runtime
    FROM rag_runtime_state rs
    WHERE rs.collection_name = p_collection_name
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown collection runtime state %', p_collection_name
            USING ERRCODE = 'P0002';
    END IF;
    IF v_runtime.runtime_state <> 'serving' THEN
        RAISE EXCEPTION 'collection % is %, lease acquisition denied',
            p_collection_name, v_runtime.runtime_state USING ERRCODE = '55000';
    END IF;

    SELECT * INTO v_lease
    FROM rag_active_execution_leases l
    WHERE l.collection_name = p_collection_name
      AND l.n8n_execution_id = p_n8n_execution_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_lease.released_at IS NOT NULL OR v_lease.expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'execution % lease is already closed',
                p_n8n_execution_id USING ERRCODE = '55000';
        END IF;
        IF v_lease.transaction_id IS DISTINCT FROM p_transaction_id
           OR v_lease.workflow_name IS DISTINCT FROM p_workflow_name THEN
            RAISE EXCEPTION 'execution % lease identity does not match',
                p_n8n_execution_id USING ERRCODE = '23505';
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
) RETURNS TABLE (
    lease_id UUID,
    expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_lease rag_active_execution_leases%ROWTYPE;
BEGIN
    IF p_lease_ttl <= interval '0 seconds'
       OR p_lease_ttl > interval '15 minutes' THEN
        RAISE EXCEPTION 'lease TTL must be greater than zero and at most 15 minutes'
            USING ERRCODE = '22023';
    END IF;
    UPDATE rag_active_execution_leases l
    SET heartbeat_at = clock_timestamp(),
        expires_at = least(
            clock_timestamp() + p_lease_ttl,
            l.hard_expires_at
        )
    WHERE l.lease_id = p_lease_id
      AND l.released_at IS NULL
      AND l.expires_at > clock_timestamp()
    RETURNING l.* INTO v_lease;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease % is missing, released, or expired', p_lease_id
            USING ERRCODE = '55000';
    END IF;
    RETURN QUERY SELECT v_lease.lease_id, v_lease.expires_at;
END
$$;

CREATE OR REPLACE FUNCTION rag_release_execution_lease(
    p_lease_id UUID
) RETURNS TABLE (
    lease_id UUID,
    released_at TIMESTAMPTZ
)
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
