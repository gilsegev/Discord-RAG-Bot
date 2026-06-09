CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS rag_transactions (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    discord_message_id TEXT,
    discord_response_message_id TEXT,
    channel_id TEXT,
    channel_name TEXT,
    author_id_hash TEXT,
    incoming_message_ts TIMESTAMPTZ,
    route_type TEXT CHECK (
        route_type IN ('active_call', 'passive_candidate', 'ignored')
    ),
    status TEXT NOT NULL DEFAULT 'created' CHECK (
        status IN (
            'created',
            'ignored',
            'retrieving',
            'refused',
            'answered',
            'failed'
        )
    ),
    retrieval_status TEXT CHECK (
        retrieval_status IN (
            'not_started',
            'context_found',
            'no_context',
            'weak_context',
            'failed'
        )
    ) DEFAULT 'not_started',
    response_status TEXT CHECK (
        response_status IN (
            'not_started',
            'posted',
            'not_posted',
            'failed'
        )
    ) DEFAULT 'not_started',
    refusal_reason TEXT,
    failure_reason TEXT,
    user_query TEXT,
    normalized_query TEXT,
    query_hash TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rag_trace_events (
    event_id BIGSERIAL PRIMARY KEY,
    transaction_id UUID REFERENCES rag_transactions(transaction_id)
        ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    node_name TEXT,
    status TEXT CHECK (
        status IN ('started', 'completed', 'failed', 'decision')
    ),
    latency_ms INTEGER,
    event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_retrieval_results (
    result_id BIGSERIAL PRIMARY KEY,
    transaction_id UUID REFERENCES rag_transactions(transaction_id)
        ON DELETE CASCADE,
    qdrant_point_id TEXT,
    rank INTEGER,
    retrieval_score DOUBLE PRECISION,
    reranker_score DOUBLE PRECISION,
    boosted_reranker_score DOUBLE PRECISION,
    channel_id TEXT,
    channel_name TEXT,
    thread_name TEXT,
    first_message_id TEXT,
    message_ids TEXT[],
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_feedback (
    feedback_id BIGSERIAL PRIMARY KEY,
    transaction_id UUID REFERENCES rag_transactions(transaction_id)
        ON DELETE CASCADE,
    discord_response_message_id TEXT NOT NULL,
    feedback_author_id_hash TEXT,
    feedback_type TEXT NOT NULL CHECK (
        feedback_type IN ('positive', 'negative', 'explicit')
    ),
    reaction_name TEXT,
    feedback_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (discord_response_message_id, feedback_author_id_hash, feedback_type)
);

CREATE TABLE IF NOT EXISTS rag_eval_labels (
    eval_label_id BIGSERIAL PRIMARY KEY,
    transaction_id UUID REFERENCES rag_transactions(transaction_id)
        ON DELETE CASCADE,
    dimension TEXT NOT NULL CHECK (
        dimension IN ('groundedness', 'answer_relevance', 'tone_refusal', 'safety')
    ),
    label TEXT NOT NULL CHECK (label IN ('pass', 'fail')),
    failure_type TEXT,
    source TEXT NOT NULL CHECK (source IN ('human', 'judge')),
    labeler TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_weekly_metrics (
    week_start DATE PRIMARY KEY,
    sample_size INTEGER NOT NULL DEFAULT 0,
    context_found_rate NUMERIC(5, 2),
    groundedness_pass_rate NUMERIC(5, 2),
    correct_refusal_rate NUMERIC(5, 2),
    thumbs_up_rate NUMERIC(5, 2),
    rag_reliability_index NUMERIC(5, 2),
    no_context_violation_count INTEGER NOT NULL DEFAULT 0,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    top_failure_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    digest_posted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_transactions_created_at
    ON rag_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_rag_transactions_status
    ON rag_transactions(status);
CREATE INDEX IF NOT EXISTS idx_rag_transactions_discord_response
    ON rag_transactions(discord_response_message_id);
CREATE INDEX IF NOT EXISTS idx_rag_transactions_query_hash
    ON rag_transactions(query_hash);
CREATE INDEX IF NOT EXISTS idx_rag_trace_events_transaction
    ON rag_trace_events(transaction_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rag_retrieval_results_transaction
    ON rag_retrieval_results(transaction_id, rank);
CREATE INDEX IF NOT EXISTS idx_rag_feedback_transaction
    ON rag_feedback(transaction_id);

CREATE OR REPLACE VIEW rag_recent_transactions AS
SELECT
    transaction_id,
    route_type,
    status,
    retrieval_status,
    response_status,
    refusal_reason,
    channel_name,
    created_at,
    completed_at,
    latency_ms
FROM rag_transactions
ORDER BY created_at DESC
LIMIT 100;

CREATE OR REPLACE VIEW rag_failed_transactions AS
SELECT *
FROM rag_transactions
WHERE status = 'failed'
   OR retrieval_status IN ('no_context', 'weak_context', 'failed')
   OR response_status = 'failed'
ORDER BY created_at DESC;

