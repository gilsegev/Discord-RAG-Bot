-- Adopt a regression-validated candidate as the durable incremental corpus.
-- Qdrant routing is changed separately; this transaction changes only Postgres
-- ownership, corpus identity, pending-work state, and scheduler targeting.

CREATE TABLE IF NOT EXISTS rag_corpus_manifest_snapshots (
    corpus_version_id TEXT NOT NULL,
    point_id TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    logical_group_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_id TEXT,
    root_message_id TEXT,
    message_ids TEXT[] NOT NULL CHECK (cardinality(message_ids) > 0),
    first_message_id TEXT NOT NULL,
    last_message_id TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    ingestion_run_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus_version_id, point_id)
);

CREATE TABLE IF NOT EXISTS rag_candidate_manifest_promotions (
    candidate_id TEXT PRIMARY KEY REFERENCES rag_candidate_rebuilds(candidate_id),
    previous_corpus_version_id TEXT NOT NULL,
    previous_collection_name TEXT NOT NULL,
    promoted_collection_name TEXT NOT NULL,
    promoted_corpus_version_id TEXT NOT NULL,
    regression_run_id UUID NOT NULL REFERENCES rag_regression_runs(run_id),
    promoted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION rag_protect_authorized_candidate_manifest()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_candidate_id TEXT := coalesce(NEW.candidate_id, OLD.candidate_id);
BEGIN
    IF EXISTS (
        SELECT 1 FROM rag_candidate_regression_authorizations
        WHERE candidate_id = v_candidate_id AND consumed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'authorized candidate manifest is immutable'
            USING ERRCODE='55000';
    END IF;
    RETURN coalesce(NEW, OLD);
END
$$;

DROP TRIGGER IF EXISTS protect_authorized_candidate_manifest
ON rag_candidate_chunk_manifest;
CREATE TRIGGER protect_authorized_candidate_manifest
BEFORE INSERT OR UPDATE OR DELETE ON rag_candidate_chunk_manifest
FOR EACH ROW EXECUTE FUNCTION rag_protect_authorized_candidate_manifest();

DROP FUNCTION IF EXISTS rag_promote_candidate_manifest(TEXT, UUID);
DROP FUNCTION IF EXISTS rag_promote_candidate_manifest(TEXT, UUID, TEXT);
CREATE OR REPLACE FUNCTION rag_promote_candidate_manifest(
    p_candidate_id TEXT,
    p_regression_run_id UUID,
    p_previous_collection_name TEXT DEFAULT 'tpm_unite_history'
) RETURNS TABLE (
    promoted_collection_name TEXT,
    promoted_corpus_version_id TEXT,
    promoted_point_count INTEGER,
    completed_pending_work INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_candidate rag_candidate_rebuilds%ROWTYPE;
    v_regression rag_regression_runs%ROWTYPE;
    v_previous rag_corpus_versions%ROWTYPE;
    v_schedule rag_incremental_schedule_config%ROWTYPE;
    v_completed INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('rag_candidate_manifest_promotion'));
    LOCK TABLE rag_discord_messages IN SHARE MODE;
    LOCK TABLE rag_pending_chunk_work IN SHARE ROW EXCLUSIVE MODE;

    SELECT * INTO v_candidate
    FROM rag_candidate_rebuilds
    WHERE candidate_id = p_candidate_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown candidate %', p_candidate_id USING ERRCODE='P0002';
    END IF;
    IF v_candidate.status <> 'regression_authorized' THEN
        RAISE EXCEPTION 'candidate is not in the accepted regression state'
            USING ERRCODE='55000';
    END IF;
    IF EXISTS (SELECT 1 FROM rag_candidate_manifest_promotions
               WHERE candidate_id = p_candidate_id) THEN
        RAISE EXCEPTION 'candidate has already been promoted'
            USING ERRCODE='55000';
    END IF;

    SELECT * INTO v_regression
    FROM rag_regression_runs
    WHERE run_id = p_regression_run_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_regression.status <> 'completed'
       OR v_regression.run_mode <> 'retrieval_only'
       OR v_regression.question_file <> 'scripts/regression_questions.jsonl'
       OR v_regression.question_file_hash <> 'ed575223baf577cdd3c15ad6398a351d2dce5ad872444daf2bdbf6b63e0b0eb7'
       OR v_regression.workflow_name <> 'RAG Regression Batch Runner - Phase 8'
       OR v_regression.workflow_version <> 'phase-8-regression-v1'
       OR v_regression.case_count <> 48
       OR v_regression.pass_count <> 43
       OR v_regression.fail_count <> 1
       OR v_regression.review_count <> 4 THEN
        RAISE EXCEPTION 'candidate promotion requires the accepted 48-case regression baseline'
            USING ERRCODE='55000';
    END IF;
    IF (SELECT count(*) FROM rag_regression_results
        WHERE run_id = p_regression_run_id) <> 48
       OR (SELECT count(DISTINCT case_id) FROM rag_regression_results
           WHERE run_id = p_regression_run_id) <> 48
       OR EXISTS (
           SELECT 1 FROM generate_series(1, 48) n
           WHERE NOT EXISTS (
               SELECT 1 FROM rag_regression_results r
               WHERE r.run_id = p_regression_run_id
                 AND r.case_id = 'RQ-' || lpad(n::text, 3, '0')
           )
       ) THEN
        RAISE EXCEPTION 'regression results are not the canonical 48 distinct cases'
            USING ERRCODE='55000';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM rag_candidate_regression_authorizations a
        WHERE a.candidate_id = v_candidate.candidate_id
          AND a.collection_name = v_candidate.collection_name
          AND a.corpus_version_id = v_candidate.corpus_version_id
          AND a.manifest_digest = v_candidate.manifest_digest
          AND a.frozen_capture_sequence = v_candidate.frozen_capture_sequence
          AND a.consumed_by_regression_run_id = p_regression_run_id
          AND a.consumed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'regression run is not bound to this candidate'
            USING ERRCODE='55000';
    END IF;

    IF v_candidate.frozen_capture_sequence IS DISTINCT FROM (
        SELECT coalesce(max(capture_sequence), 0) FROM rag_discord_messages
    ) THEN
        RAISE EXCEPTION 'candidate is stale relative to captured messages'
            USING ERRCODE='55000';
    END IF;

    IF (SELECT count(*) FROM rag_candidate_chunk_manifest
        WHERE candidate_id = p_candidate_id) <> v_candidate.point_count THEN
        RAISE EXCEPTION 'candidate manifest point count does not match build evidence'
            USING ERRCODE='55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM rag_candidate_chunk_manifest m
        WHERE m.candidate_id = p_candidate_id
          AND (m.collection_name <> v_candidate.collection_name
               OR m.corpus_version_id <> v_candidate.corpus_version_id
               OR btrim(m.payload_digest) = '')
    ) OR coalesce(v_candidate.evidence->'structural_audit'->>'passed','false') <> 'true'
    THEN
        RAISE EXCEPTION 'candidate manifest integrity checks failed'
            USING ERRCODE='55000';
    END IF;

    SELECT * INTO v_previous
    FROM rag_corpus_versions
    WHERE status = 'healthy'
      AND collection_name = p_previous_collection_name
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no healthy serving corpus exists' USING ERRCODE='55000';
    END IF;
    IF (SELECT count(*) FROM rag_corpus_versions WHERE status='healthy') <> 1
       OR EXISTS (SELECT 1 FROM rag_chunk_manifest
                  WHERE active AND collection_name <> v_previous.collection_name) THEN
        RAISE EXCEPTION 'serving corpus identity is ambiguous'
            USING ERRCODE='55000';
    END IF;

    PERFORM 1 FROM rag_runtime_state
    WHERE collection_name IN (v_previous.collection_name,
                              v_candidate.collection_name)
    FOR UPDATE;
    IF (SELECT count(*) FROM rag_runtime_state
        WHERE collection_name IN (v_previous.collection_name,
                                  v_candidate.collection_name)
          AND runtime_state='serving'
          AND active_incremental_run_id IS NULL) <> 2
       OR EXISTS (
           SELECT 1 FROM rag_active_execution_leases
           WHERE collection_name IN (v_previous.collection_name,
                                     v_candidate.collection_name)
             AND released_at IS NULL AND expires_at > clock_timestamp()
       )
       OR EXISTS (
           SELECT 1 FROM rag_incremental_runs
           WHERE collection_name IN (v_previous.collection_name,
                                     v_candidate.collection_name)
             AND run_state NOT IN ('completed','failed')
       ) THEN
        RAISE EXCEPTION 'promotion requires drained serving runtimes with no active incremental run'
            USING ERRCODE='55000';
    END IF;

    SELECT * INTO v_schedule
    FROM rag_incremental_schedule_config
    WHERE collection_name = v_previous.collection_name
    FOR UPDATE;
    IF NOT FOUND OR v_schedule.schedule_enabled THEN
        RAISE EXCEPTION 'previous incremental schedule must exist and be disabled'
            USING ERRCODE='55000';
    END IF;

    INSERT INTO rag_corpus_manifest_snapshots (
        corpus_version_id, point_id, collection_name, logical_group_id,
        channel_id, thread_id, root_message_id, message_ids,
        first_message_id, last_message_id, chunker_version,
        embedding_version, ingestion_run_id, payload_digest
    )
    SELECT v_previous.corpus_version_id, m.point_id, m.collection_name,
        m.logical_group_id, m.channel_id, m.thread_id, m.root_message_id,
        m.message_ids, m.first_message_id, m.last_message_id,
        m.chunker_version, m.embedding_version, m.ingestion_run_id,
        m.payload_digest
    FROM rag_chunk_manifest m
    WHERE m.active
    ON CONFLICT (corpus_version_id, point_id) DO NOTHING;

    INSERT INTO rag_ingestion_runs (
        run_id, run_kind, status, collection_name, chunker_version,
        embedding_version, point_count, manifest_digest, completed_at
    ) VALUES (
        v_candidate.candidate_id, 'baseline_seed', 'completed',
        v_candidate.collection_name, v_candidate.chunker_version,
        v_candidate.embedding_version, v_candidate.point_count,
        v_candidate.manifest_digest, now()
    ) ON CONFLICT (run_id) DO NOTHING;

    DELETE FROM rag_chunk_message_ownership
    WHERE point_id IN (
        SELECT point_id FROM rag_chunk_manifest WHERE active
        UNION
        SELECT point_id FROM rag_candidate_chunk_manifest
        WHERE candidate_id = p_candidate_id
    );
    UPDATE rag_chunk_manifest
    SET active = false, superseded_at = now()
    WHERE active;

    INSERT INTO rag_chunk_manifest (
        point_id, collection_name, logical_group_id, channel_id, thread_id,
        root_message_id, message_ids, first_message_id, last_message_id,
        chunker_version, embedding_version, ingestion_run_id,
        payload_digest, active
    )
    SELECT m.point_id, v_candidate.collection_name, m.logical_group_id,
        m.channel_id, m.thread_id, m.root_message_id, m.message_ids,
        m.first_message_id, m.last_message_id, v_candidate.chunker_version,
        v_candidate.embedding_version, v_candidate.candidate_id,
        m.payload_digest, true
    FROM rag_candidate_chunk_manifest m
    WHERE m.candidate_id = p_candidate_id
    ON CONFLICT (point_id) DO UPDATE SET
        collection_name = excluded.collection_name,
        logical_group_id = excluded.logical_group_id,
        channel_id = excluded.channel_id,
        thread_id = excluded.thread_id,
        root_message_id = excluded.root_message_id,
        message_ids = excluded.message_ids,
        first_message_id = excluded.first_message_id,
        last_message_id = excluded.last_message_id,
        chunker_version = excluded.chunker_version,
        embedding_version = excluded.embedding_version,
        ingestion_run_id = excluded.ingestion_run_id,
        payload_digest = excluded.payload_digest,
        active = true,
        superseded_at = NULL;

    INSERT INTO rag_chunk_message_ownership (
        point_id, message_id, message_position
    )
    SELECT m.point_id, u.message_id, u.ordinality - 1
    FROM rag_candidate_chunk_manifest m
    CROSS JOIN LATERAL unnest(m.message_ids)
        WITH ORDINALITY u(message_id, ordinality)
    WHERE m.candidate_id = p_candidate_id;

    UPDATE rag_corpus_versions
    SET status = 'superseded', superseded_at = now()
    WHERE corpus_version_id = v_previous.corpus_version_id;
    INSERT INTO rag_corpus_versions (
        corpus_version_id, ingestion_run_id, collection_name,
        manifest_digest, point_count, status, activated_at
    ) VALUES (
        v_candidate.corpus_version_id, v_candidate.candidate_id,
        v_candidate.collection_name, v_candidate.manifest_digest,
        v_candidate.point_count, 'healthy', now()
    ) ON CONFLICT (corpus_version_id) DO UPDATE SET
        status = 'healthy', activated_at = now(), superseded_at = NULL;

    INSERT INTO rag_runtime_state (collection_name, runtime_state)
    VALUES (v_candidate.collection_name, 'serving')
    ON CONFLICT (collection_name) DO UPDATE SET
        runtime_state = 'serving', active_incremental_run_id = NULL,
        state_revision = rag_runtime_state.state_revision + 1,
        changed_at = now();

    INSERT INTO rag_incremental_schedule_config (
        collection_name, schedule_enabled, catchup_completed,
        cron_expression, schedule_timezone, max_messages_per_run,
        max_replacement_points, max_estimated_seconds,
        max_maintenance_seconds, success_alerts_enabled, config_metadata
    )
    SELECT v_candidate.collection_name, true, true, cron_expression,
        schedule_timezone, max_messages_per_run, max_replacement_points,
        max_estimated_seconds, max_maintenance_seconds,
        success_alerts_enabled,
        config_metadata || jsonb_build_object(
            'promoted_candidate_id', v_candidate.candidate_id,
            'promoted_at', now()
        )
    FROM rag_incremental_schedule_config
    WHERE collection_name = v_previous.collection_name
    ON CONFLICT (collection_name) DO UPDATE SET
        schedule_enabled = true,
        catchup_completed = true,
        cron_expression = excluded.cron_expression,
        schedule_timezone = excluded.schedule_timezone,
        max_messages_per_run = excluded.max_messages_per_run,
        max_replacement_points = excluded.max_replacement_points,
        max_estimated_seconds = excluded.max_estimated_seconds,
        max_maintenance_seconds = excluded.max_maintenance_seconds,
        success_alerts_enabled = excluded.success_alerts_enabled,
        updated_at = now(),
        config_metadata = excluded.config_metadata;

    UPDATE rag_incremental_schedule_config
    SET schedule_enabled = false, updated_at = now()
    WHERE collection_name <> v_candidate.collection_name;
    IF (SELECT count(*) FROM rag_incremental_schedule_config
        WHERE schedule_enabled) <> 1
       OR NOT EXISTS (
           SELECT 1 FROM rag_incremental_schedule_config
           WHERE collection_name = v_candidate.collection_name
             AND schedule_enabled
       ) THEN
        RAISE EXCEPTION 'candidate incremental schedule was not enabled exclusively'
            USING ERRCODE='55000';
    END IF;

    UPDATE rag_pending_chunk_work
    SET status = 'completed', claimed_at = NULL, completed_at = now(),
        failure_reason = NULL
    WHERE capture_sequence <= v_candidate.frozen_capture_sequence
      AND status <> 'completed';
    GET DIAGNOSTICS v_completed = ROW_COUNT;

    INSERT INTO rag_candidate_manifest_promotions (
        candidate_id, previous_corpus_version_id,
        previous_collection_name, promoted_collection_name,
        promoted_corpus_version_id, regression_run_id
    ) VALUES (
        v_candidate.candidate_id, v_previous.corpus_version_id,
        v_previous.collection_name, v_candidate.collection_name,
        v_candidate.corpus_version_id, p_regression_run_id
    ) ON CONFLICT (candidate_id) DO NOTHING;

    RETURN QUERY SELECT v_candidate.collection_name,
        v_candidate.corpus_version_id, v_candidate.point_count, v_completed;
END
$$;
