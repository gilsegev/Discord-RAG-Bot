ALTER TABLE rag_transactions
    ADD COLUMN IF NOT EXISTS failure_reason TEXT;

ALTER TABLE rag_transactions
    ADD COLUMN IF NOT EXISTS query_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_rag_transactions_query_hash
    ON rag_transactions(query_hash);
