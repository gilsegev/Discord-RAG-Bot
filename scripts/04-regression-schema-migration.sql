CREATE EXTENSION IF NOT EXISTS pgcrypto;

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

CREATE INDEX IF NOT EXISTS idx_rag_regression_runs_started
    ON rag_regression_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_regression_results_run
    ON rag_regression_results(run_id, case_id);
CREATE INDEX IF NOT EXISTS idx_rag_regression_results_outcome
    ON rag_regression_results(outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_regression_results_transaction
    ON rag_regression_results(transaction_id);
