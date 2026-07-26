-- Phase 9C.1: capture eligible Discord messages before RAG routing.

CREATE TABLE IF NOT EXISTS rag_discord_messages (
    message_id TEXT PRIMARY KEY,
    capture_sequence BIGSERIAL NOT NULL UNIQUE,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    parent_channel_id TEXT NOT NULL,
    parent_channel_name TEXT NOT NULL,
    thread_id TEXT,
    thread_name TEXT,
    parent_message_id TEXT,
    author_id_hash TEXT NOT NULL,
    author_display_name TEXT NOT NULL,
    content TEXT NOT NULL,
    message_created_at TIMESTAMPTZ NOT NULL,
    message_type TEXT NOT NULL,
    has_attachments BOOLEAN NOT NULL DEFAULT false,
    normalizer_version TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_pending_chunk_work (
    work_id BIGSERIAL PRIMARY KEY,
    source_message_id TEXT NOT NULL UNIQUE
        REFERENCES rag_discord_messages(message_id) ON DELETE CASCADE,
    capture_sequence BIGINT NOT NULL UNIQUE,
    work_kind TEXT NOT NULL CHECK (
        work_kind IN ('reply_conversation', 'recent_window')
    ),
    parent_channel_id TEXT NOT NULL,
    thread_id TEXT,
    parent_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'claimed', 'completed', 'failed')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_discord_messages_parent
    ON rag_discord_messages(parent_message_id);
CREATE INDEX IF NOT EXISTS idx_rag_discord_messages_scope_time
    ON rag_discord_messages(
        parent_channel_id,
        thread_id,
        message_created_at
    );
CREATE INDEX IF NOT EXISTS idx_rag_pending_chunk_work_status_sequence
    ON rag_pending_chunk_work(status, capture_sequence);
