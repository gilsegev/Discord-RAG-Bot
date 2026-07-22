-- Phase 10: one current reaction vote per member, response, and source.

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Preserve the newest signal if legacy data contains both positive and negative
-- reaction rows for one member and response.
WITH ranked AS (
    SELECT feedback_id,
           row_number() OVER (
               PARTITION BY discord_response_message_id,
                            feedback_author_id_hash,
                            feedback_source
               ORDER BY updated_at DESC, created_at DESC, feedback_id DESC
           ) AS row_rank
    FROM rag_feedback
)
DELETE FROM rag_feedback feedback
USING ranked
WHERE feedback.feedback_id = ranked.feedback_id
  AND ranked.row_rank > 1;

DROP INDEX IF EXISTS idx_rag_feedback_unique_signal;

-- The original three-column UNIQUE constraint was created with a truncated
-- PostgreSQL-generated name in early deployments.
ALTER TABLE rag_feedback
    DROP CONSTRAINT IF EXISTS rag_feedback_discord_response_message_id_feedback_author_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_feedback_unique_current_signal
    ON rag_feedback(
        discord_response_message_id,
        feedback_author_id_hash,
        feedback_source
    );

CREATE INDEX IF NOT EXISTS idx_rag_feedback_unmatched_response
    ON rag_feedback(discord_response_message_id)
    WHERE matched = false;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_transactions_unique_response_message
    ON rag_transactions(discord_response_message_id)
    WHERE discord_response_message_id IS NOT NULL;
