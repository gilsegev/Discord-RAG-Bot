-- Phase 9C.2: durable ownership for the baseline Qdrant corpus.

CREATE TABLE IF NOT EXISTS rag_ingestion_runs (
    run_id TEXT PRIMARY KEY,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('baseline_seed', 'incremental')),
    status TEXT NOT NULL CHECK (
        status IN ('preparing', 'validated', 'applying', 'completed', 'failed')
    ),
    collection_name TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    point_count INTEGER NOT NULL CHECK (point_count >= 0),
    manifest_digest TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS rag_corpus_versions (
    corpus_version_id TEXT PRIMARY KEY,
    ingestion_run_id TEXT NOT NULL REFERENCES rag_ingestion_runs(run_id),
    collection_name TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    point_count INTEGER NOT NULL CHECK (point_count >= 0),
    status TEXT NOT NULL CHECK (
        status IN ('preparing', 'healthy', 'review_needed', 'superseded')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rag_chunk_manifest (
    point_id TEXT PRIMARY KEY,
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
    ingestion_run_id TEXT NOT NULL REFERENCES rag_ingestion_runs(run_id),
    payload_digest TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    superseded_at TIMESTAMPTZ,
    CHECK ((active AND superseded_at IS NULL) OR NOT active)
);

CREATE TABLE IF NOT EXISTS rag_chunk_message_ownership (
    point_id TEXT NOT NULL REFERENCES rag_chunk_manifest(point_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    message_position INTEGER NOT NULL CHECK (message_position >= 0),
    PRIMARY KEY (point_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunk_manifest_group_active
    ON rag_chunk_manifest(collection_name, logical_group_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_rag_chunk_manifest_root_active
    ON rag_chunk_manifest(root_message_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_rag_chunk_manifest_channel_active
    ON rag_chunk_manifest(collection_name, channel_id, thread_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_rag_chunk_message_ownership_message
    ON rag_chunk_message_ownership(message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_corpus_versions_one_healthy
    ON rag_corpus_versions(collection_name) WHERE status = 'healthy';
