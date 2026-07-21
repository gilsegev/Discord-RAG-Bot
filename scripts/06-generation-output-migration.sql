-- Add durable generation output to the transaction source of truth.
ALTER TABLE rag_transactions
    ADD COLUMN IF NOT EXISTS generated_answer TEXT,
    ADD COLUMN IF NOT EXISTS final_response_text TEXT,
    ADD COLUMN IF NOT EXISTS generation_model TEXT,
    ADD COLUMN IF NOT EXISTS generation_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
