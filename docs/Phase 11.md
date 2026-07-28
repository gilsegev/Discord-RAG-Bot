# Phase 11: Discord Reaction Feedback Correlation

## Scope

Phase 11 provides shared feedback plumbing for every bot response whose
`discord_response_message_id` is persisted. It is not gated on Phase 9B and
works for active calls, passive responses, and future response modes.
Those response IDs come from the n8n Discord Bot API output writer, which posts
to the originating channel; Phase 11 does not depend on channel webhooks.

Version 1 captures Unicode 👍 and 👎 reactions, including skin-tone variants.
Explicit context-menu critiques
are deferred to Phase 11B because the referenced Feedback & Reaction
Correlation Design is not currently present in this repository.

The deployed workflow, webhook, migration, and filenames retain their original
`phase-10` identifiers for backward compatibility. Those identifiers are stable
runtime names, not the current roadmap phase number.

## Reaction contract

Each Discord member's configured reactions are retained independently per bot
response. Different members also retain independent reactions. A member may
therefore have both a positive and a negative reaction on the same response,
matching Discord's actual reaction state.

| Event | Existing reactions by the same member | Result |
|---|---|---|
| Add 👍 | none | Store positive vote |
| Add 👎 | none | Store negative vote and flag it for review |
| Add 👎 | 👍 | Keep 👍 and store a separate negative vote; flag the negative row for review |
| Add 👍 | 👎 | Keep 👎 and store a separate positive vote |
| Remove 👍 | 👍 and 👎 | Remove only the positive row; retain the negative row |
| Remove 👎 | 👍 and 👎 | Remove only the negative row; retain the positive row |
| Remove absent emoji | no matching row | No-op |
| Repeat same event | same state | Idempotent upsert or no-op |

Negative feedback is a satisfaction signal and human-review trigger. It never
writes a pass/fail label to `rag_eval_labels`.

## Runtime flow

```text
Discord GUILD_MESSAGE_REACTIONS event
-> ragbot-discord-listener
-> POST /webhook/rag-feedback-phase-10
-> RAG Feedback Correlation - Phase 10 (legacy runtime name)
-> correlate rag_transactions.discord_response_message_id
-> upsert/remove the matching rag_feedback reaction row
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
scope is needed for Phase 11 v1. The bot must be able to view the response
channels and read message history.

## Verification

Test with two members on one stored bot response: A adds 👍 and 👎, B adds 👎,
then A removes 👍. Verify that A's negative row and B's negative row remain and
that the matching `feedback.linked` trace events exist. Weekly metrics should
derive member-level positive, negative, and mixed states from current rows.
