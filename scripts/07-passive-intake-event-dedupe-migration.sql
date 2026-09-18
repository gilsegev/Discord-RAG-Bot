-- Claim each delivered Discord message ID independently of corpus eligibility.
-- Repeated wording under a new message ID is a new event and remains processable.
CREATE TABLE IF NOT EXISTS rag_intake_event_claims (
    guild_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, message_id)
);
