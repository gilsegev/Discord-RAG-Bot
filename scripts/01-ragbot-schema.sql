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
    dedupe_status TEXT,
    dedupe_reason TEXT,
    dedupe_matched_chunk_id TEXT,
    dedupe_overlap_ratio DOUBLE PRECISION,
    dedupe_shared_message_count INTEGER,
    rank_after_dedupe INTEGER,
    selected_for_context BOOLEAN,
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
    feedback_source TEXT NOT NULL DEFAULT 'reaction' CHECK (
        feedback_source IN (
            'reaction',
            'context_menu',
            'slash_command',
            'form',
            'manual'
        )
    ),
    feedback_value TEXT NOT NULL DEFAULT 'unknown',
    feedback_type TEXT NOT NULL CHECK (
        feedback_type IN ('positive', 'negative', 'explicit')
    ),
    reaction_name TEXT,
    feedback_text TEXT,
    feedback_category TEXT CHECK (
        feedback_category IN (
            'made_something_up',
            'did_not_answer',
            'wrong_tone',
            'surfaced_personal_info',
            'other'
        )
    ),
    matched BOOLEAN NOT NULL DEFAULT true,
    review_candidate BOOLEAN NOT NULL DEFAULT false,
    review_status TEXT CHECK (
        review_status IN ('pending', 'in_review', 'resolved', 'dismissed')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (review_candidate = false OR review_status IS NOT NULL),
    UNIQUE (
        discord_response_message_id,
        feedback_author_id_hash,
        feedback_source,
        feedback_type
    )
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
    source TEXT NOT NULL CHECK (source IN ('human', 'judge', 'regression')),
    labeler TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_regression_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_source TEXT NOT NULL CHECK (
        trigger_source IN ('regression_manual', 'regression_ci', 'evaluator_manual')
    ),
    run_mode TEXT NOT NULL CHECK (
        run_mode IN ('retrieval_only', 'full_answer')
    ),
    question_file TEXT NOT NULL,
    question_file_hash TEXT NOT NULL,
    workflow_name TEXT,
    workflow_version TEXT,
    git_sha TEXT,
    status TEXT NOT NULL DEFAULT 'started' CHECK (
        status IN ('started', 'completed', 'failed', 'cancelled')
    ),
    case_count INTEGER NOT NULL DEFAULT 0,
    pass_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rag_regression_results (
    result_id BIGSERIAL PRIMARY KEY,
    run_id UUID REFERENCES rag_regression_runs(run_id)
        ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    category TEXT,
    question TEXT NOT NULL,
    expected_action TEXT,
    expected_caveat TEXT,
    expected_flags TEXT,
    expected_behavior TEXT,
    transaction_id UUID REFERENCES rag_transactions(transaction_id)
        ON DELETE SET NULL,
    trace_id TEXT,
    actual_status TEXT,
    retrieval_status TEXT,
    refusal_reason TEXT,
    selected_context_count INTEGER,
    selected_channels TEXT[],
    selected_chunk_ids TEXT[],
    retrieval_scores DOUBLE PRECISION[],
    reranker_scores DOUBLE PRECISION[],
    context_token_estimate INTEGER,
    answer_length INTEGER,
    citation_status TEXT,
    latency_ms INTEGER,
    outcome TEXT NOT NULL CHECK (
        outcome IN (
            'pass',
            'review_needed',
            'false_refusal',
            'missed_refusal',
            'no_context_violation',
            'citation_failure',
            'context_assembly_failure',
            'pii_safety_failure',
            'workflow_failure'
        )
    ),
    failure_type TEXT,
    review_notes TEXT,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
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
CREATE INDEX IF NOT EXISTS idx_rag_feedback_review
    ON rag_feedback(review_candidate, review_status)
    WHERE review_candidate;
CREATE INDEX IF NOT EXISTS idx_rag_regression_runs_started
    ON rag_regression_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_regression_results_run
    ON rag_regression_results(run_id, case_id);
CREATE INDEX IF NOT EXISTS idx_rag_regression_results_outcome
    ON rag_regression_results(outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_regression_results_transaction
    ON rag_regression_results(transaction_id);

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

