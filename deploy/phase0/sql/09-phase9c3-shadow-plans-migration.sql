-- Phase 9C.3: durable, read-only shadow rechunk plans.

CREATE TABLE IF NOT EXISTS rag_chunk_replacement_plans (
    plan_id TEXT PRIMARY KEY,
    collection_name TEXT NOT NULL,
    batch_cutoff_sequence BIGINT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('planned', 'shadow_validated', 'deferred', 'failed')
    ),
    plan_digest TEXT NOT NULL,
    old_point_count INTEGER NOT NULL CHECK (old_point_count >= 0),
    replacement_point_count INTEGER NOT NULL CHECK (replacement_point_count >= 0),
    pending_message_count INTEGER NOT NULL CHECK (pending_message_count >= 0),
    estimated_seconds NUMERIC,
    measured_embedding_seconds NUMERIC,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    validated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rag_chunk_replacement_plan_groups (
    plan_id TEXT NOT NULL
        REFERENCES rag_chunk_replacement_plans(plan_id) ON DELETE CASCADE,
    group_key TEXT NOT NULL,
    work_kind TEXT NOT NULL CHECK (
        work_kind IN ('reply_conversation', 'recent_window')
    ),
    channel_id TEXT NOT NULL,
    thread_id TEXT,
    root_message_id TEXT,
    source_message_ids TEXT[] NOT NULL,
    old_point_ids TEXT[] NOT NULL,
    replacement_point_ids TEXT[] NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'deferred')),
    evidence JSONB NOT NULL,
    PRIMARY KEY (plan_id, group_key)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunk_replacement_plans_status
    ON rag_chunk_replacement_plans(status, created_at);
CREATE INDEX IF NOT EXISTS idx_rag_chunk_replacement_groups_scope
    ON rag_chunk_replacement_plan_groups(channel_id, thread_id);
