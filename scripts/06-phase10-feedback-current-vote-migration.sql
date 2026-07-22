-- Phase 10: retain each distinct reaction from a member on a response.

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Remove only duplicate copies of the same normalized reaction. Positive and
-- negative reactions from the same member intentionally remain separate rows.
WITH ranked AS (
    SELECT feedback_id,
           row_number() OVER (
               PARTITION BY discord_response_message_id,
                            feedback_author_id_hash,
                            feedback_source,
                            feedback_value
               ORDER BY updated_at DESC, created_at DESC, feedback_id DESC
           ) AS row_rank
    FROM rag_feedback
)
DELETE FROM rag_feedback feedback
USING ranked
WHERE feedback.feedback_id = ranked.feedback_id
  AND ranked.row_rank > 1;

DROP INDEX IF EXISTS idx_rag_feedback_unique_signal;
DROP INDEX IF EXISTS idx_rag_feedback_unique_current_signal;

-- The original three-column UNIQUE constraint was created with a truncated
-- PostgreSQL-generated name in early deployments.
ALTER TABLE rag_feedback
    DROP CONSTRAINT IF EXISTS rag_feedback_discord_response_message_id_feedback_author_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_feedback_unique_current_signal
    ON rag_feedback(
        discord_response_message_id,
        feedback_author_id_hash,
        feedback_source,
        feedback_value
    );

CREATE INDEX IF NOT EXISTS idx_rag_feedback_unmatched_response
    ON rag_feedback(discord_response_message_id)
    WHERE matched = false;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_transactions_unique_response_message
    ON rag_transactions(discord_response_message_id)
    WHERE discord_response_message_id IS NOT NULL;
