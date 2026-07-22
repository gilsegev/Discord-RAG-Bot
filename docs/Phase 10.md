# Phase 10: Discord Reaction Feedback Correlation

## Scope

Phase 10 provides shared feedback plumbing for every bot response whose
`discord_response_message_id` is persisted. It is not gated on Phase 9B and
works for active calls, passive responses, and future response modes.

Version 1 captures Unicode 👍 and 👎 reactions. Explicit context-menu critiques
are deferred to Phase 10B because the referenced Feedback & Reaction
Correlation Design is not currently present in this repository.

## Current-vote contract

Each Discord member has one current reaction vote per bot response. Different
members retain independent votes, including differing opinions.

| Event | Current vote by the same member | Result |
|---|---|---|
| Add 👍 | none | Store positive vote |
| Add 👎 | none | Store negative vote and flag it for review |
| Add 👎 | positive | Replace that member's vote with negative |
| Add 👍 | negative | Replace that member's vote with positive |
| Remove current emoji | matching vote | Remove that member's vote |
| Remove old emoji | different vote | No-op |
| Repeat same event | same state | Idempotent upsert or no-op |

Negative feedback is a satisfaction signal and human-review trigger. It never
writes a pass/fail label to `rag_eval_labels`.

## Runtime flow

```text
Discord GUILD_MESSAGE_REACTIONS event
-> ragbot-discord-listener
-> POST /webhook/rag-feedback-phase-10
-> RAG Feedback Correlation - Phase 10
-> correlate rag_transactions.discord_response_message_id
-> upsert/remove rag_feedback current vote
-> rag_trace_events + Phoenix
```

The listener hashes the reacting member's Discord ID using the existing salted
SHA-256 author-hash function. It does not forward the raw ID or nickname.

## Deployment

From the repository root, apply the migration, push and activate both workflows,
then rebuild the listener:

```bash
cd ~/Discord-RAG-Bot
docker compose -f deploy/phase0/docker-compose.yml exec -T postgres \
  psql -U ragbot_admin -d ragbot \
  < deploy/phase0/sql/06-phase10-feedback-current-vote-migration.sql

npm run n8n:push -- workflows/n8n/rag-feedback-correlation-phase-10.json
npm run n8n:push -- workflows/n8n/rag-feedback-reconcile-phase-10.json

cd deploy/phase0
docker compose --profile discord-listener up -d --build discord-listener
```

The reaction intent is not privileged. No context-menu command or new OAuth
scope is needed for Phase 10 v1. The bot must be able to view the response
channels and read message history.

## Verification

Test with two members on one stored bot response: A adds 👍, B adds 👎, A changes
to 👎, then A removes 👎. Verify the current rows and `feedback.linked` trace
events in Postgres. Weekly metrics should aggregate all members' current rows.
