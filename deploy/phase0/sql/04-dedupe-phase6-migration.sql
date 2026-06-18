ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS dedupe_status TEXT;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS dedupe_reason TEXT;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS dedupe_matched_chunk_id TEXT;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS dedupe_overlap_ratio DOUBLE PRECISION;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS dedupe_shared_message_count INTEGER;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS rank_after_dedupe INTEGER;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS selected_for_context BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_rag_retrieval_results_dedupe
    ON rag_retrieval_results(transaction_id, dedupe_status, rank_after_dedupe);
