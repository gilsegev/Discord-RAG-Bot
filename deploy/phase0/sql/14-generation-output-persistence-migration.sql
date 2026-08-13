-- Restore durable RAG generation output on the current Phase 9 transaction
-- contract. This is intentionally additive and idempotent because production
-- may already contain these columns from the earlier Oracle-era migration.
ALTER TABLE rag_transactions
    ADD COLUMN IF NOT EXISTS generated_answer TEXT,
    ADD COLUMN IF NOT EXISTS final_response_text TEXT,
    ADD COLUMN IF NOT EXISTS generation_model TEXT,
    ADD COLUMN IF NOT EXISTS generation_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN rag_transactions.generated_answer IS
    'Raw non-empty Gemini response before final response guards.';
COMMENT ON COLUMN rag_transactions.final_response_text IS
    'Final guarded response for full-answer runs; null for retrieval-only runs.';
COMMENT ON COLUMN rag_transactions.generation_model IS
    'Generation model used when Gemini produced a non-empty response.';
COMMENT ON COLUMN rag_transactions.generation_metadata IS
    'Durable generation finish, usage, latency, citation-guard, and truncation metadata.';
