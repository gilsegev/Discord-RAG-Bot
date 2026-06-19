-- 05-feedback-correlation-migration.sql
-- Feedback correlation schema update.
--
-- Feedback schema decision:
-- - feedback_type remains the existing normalized type:
--   positive | negative | explicit
-- - feedback_source stores the channel/source:
--   reaction | context_menu | slash_command | form | manual
-- - feedback_value stores the normalized sentiment or structured value.
--
-- New feedback workflows should write all three fields. Existing rows are
-- backfilled with feedback_source='reaction' and feedback_value=feedback_type.

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS feedback_source TEXT NOT NULL DEFAULT 'reaction'
        CHECK (feedback_source IN (
            'reaction',
            'context_menu',
            'slash_command',
            'form',
            'manual'
        ));

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS feedback_value TEXT NOT NULL DEFAULT 'unknown';

UPDATE rag_feedback
SET feedback_value = feedback_type
WHERE feedback_value = 'unknown'
  AND feedback_type IS NOT NULL;

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS feedback_category TEXT
        CHECK (feedback_category IN (
            'made_something_up',
            'did_not_answer',
            'wrong_tone',
            'surfaced_personal_info',
            'other'
        ));

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS matched BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS review_candidate BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE rag_feedback
    ADD COLUMN IF NOT EXISTS review_status TEXT
        CHECK (review_status IN ('pending', 'in_review', 'resolved', 'dismissed'));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_rag_feedback_review_status_required'
    ) THEN
        ALTER TABLE rag_feedback
            ADD CONSTRAINT chk_rag_feedback_review_status_required
            CHECK (review_candidate = false OR review_status IS NOT NULL);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_rag_feedback_review
    ON rag_feedback(review_candidate, review_status)
    WHERE review_candidate;

ALTER TABLE rag_feedback
    DROP CONSTRAINT IF EXISTS rag_feedback_discord_response_message_id_feedback_author_id_hash_feedback_type_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_feedback_unique_signal
    ON rag_feedback(
        discord_response_message_id,
        feedback_author_id_hash,
        feedback_source,
        feedback_type
    );
