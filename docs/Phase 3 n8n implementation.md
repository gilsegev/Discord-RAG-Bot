# Phase 3 n8n Implementation
**Status:** Draft
**Scope:** Import and test node-level observability for the active-call path
**Related:** n8n Execution Plan, Architecture Overview, Retrieval Context Prompt Contracts, Observability Design

## Purpose
This workflow adds Phase 3 node-level observability to the working active-call RAG path.

It keeps the Phase 2 functional path:

```text
manual active call -> create transaction -> normalize query -> embed query -> query Qdrant -> apply retrieval threshold -> assemble context -> call Gemini -> post Discord response -> finalize transaction
```

It adds durable trace evidence for each major step so failures are visible from Postgres without manually stepping through n8n.

## Workflow Artifact
Import this file into n8n:

```text
workflows/n8n/rag-active-call-phase-3-node-observability.json
```

Workflow name:

```text
RAG Active Call - Phase 3 Node Observability
```

## Server-Specific Defaults
The workflow is preconfigured with the current server service IPs:

| Service | URL |
|---|---|
| Qdrant | `http://172.20.0.3:6333` |
| Embedder | `http://172.20.0.6:8000/embed` |

Postgres still uses the n8n credential `Postgres account`. On the current server that credential should point to:

```text
Host: 172.20.0.2
Port: 5432
Database: ragbot
User: ragbot_admin
SSL: disabled
```

Before using `failure_reason` and durable query grouping, apply the Phase 3 observability migration:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec -T postgres psql -U ragbot_admin -d ragbot < sql/02-observability-phase3-migration.sql
```

## Values To Replace In n8n
Before running, update the `Set Manual Active Call` node:

```text
gemini_url = https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key=<your key>
discord_webhook_url = <your Discord webhook URL>?wait=true
```

Rotate any key or webhook that has been pasted into chat or screenshots.

## What It Logs
The workflow writes durable events to `rag_trace_events`:

- `discord.event_received`
- `routing.active_call`
- `query.normalized`
- `qdrant.query_started`
- `query.embedding_completed`
- `qdrant.query_completed` or `qdrant.no_context`
- `context.assembled` or `context.insufficient`
- `gemini.request_started`
- `gemini.response_completed` or `gemini.failed`
- `discord.response_sent` or `discord.response_failed`
- `transaction.phase_3_completed`

Each major event includes latency where available and an event payload with the key input/output summary.

## Contract Alignment
This workflow follows the current contracts for Phase 3:

- Uses `search_query:` for query embedding.
- Uses Nomic Embed v1.5 through the local embedder.
- Queries Qdrant with `top_k = 20`.
- Applies `retrieval_score >= 0.55`.
- Refuses if fewer than 3 results pass threshold.
- Assembles up to 5 context chunks.
- Uses the exact refusal string from the prompt contract.
- Uses `gemini-3.5-flash` in the Gemini URL.
- Records Gemini API failures as operational failures, not retrieval refusals.
- Separates `refusal_reason` from `failure_reason`: retrieval/context refusals use `refusal_reason`; API, dispatch, timeout, or malformed-response failures use `failure_reason`.

Current intentional limitations:

- No CrossEncoder reranker yet.
- No dedupe yet.
- No reaction boost yet.
- No passive listener yet.
- No feedback correlation yet.

Because reranking is not in Phase 3, context uses `retrieval_score` as the temporary score and logs `score_source = retrieval_score_until_rerank_phase`.

## Expected Success Result
If retrieval, Gemini, and Discord all succeed:

```text
status = answered
retrieval_status = context_found
response_status = posted
refusal_reason = null
discord_response_message_id = <id>
```

## Expected Refusal Result
If retrieval is missing or too weak:

```text
status = refused
retrieval_status = no_context
response_status = posted
refusal_reason = no_qdrant_results or fewer_than_min_results
```

## Expected Gemini Failure Result
If Gemini fails:

```text
status = failed
retrieval_status = context_found
response_status = failed
failure_reason = gemini_api_failed, gemini_model_not_found, gemini_auth_failed, or gemini_malformed_response
```

The workflow should not post the retrieval-refusal text when Gemini itself fails.

## Expected Discord Failure Result
If Gemini succeeds but Discord dispatch fails:

```text
status = failed
retrieval_status = context_found
response_status = failed
failure_reason = discord_dispatch_failed
discord_response_message_id = null
```

The workflow should not count this as `answered`, because the user did not receive the answer.

## Validation Queries
Inspect the latest transaction:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT transaction_id, status, retrieval_status, response_status,
       refusal_reason, failure_reason, discord_response_message_id, latency_ms, created_at
FROM rag_transactions
ORDER BY created_at DESC
LIMIT 5;"
```

Inspect trace events:

```bash
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT event_name, node_name, status, latency_ms, event_payload, created_at
FROM rag_trace_events
WHERE transaction_id = (
  SELECT transaction_id
  FROM rag_transactions
  ORDER BY created_at DESC
  LIMIT 1
)
ORDER BY created_at ASC;"
```

Inspect retrieval candidates:

```bash
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT rank, retrieval_score, channel_name, first_message_id,
       left(payload->>'text', 300) AS text_preview
FROM rag_retrieval_results
WHERE transaction_id = (
  SELECT transaction_id
  FROM rag_transactions
  ORDER BY created_at DESC
  LIMIT 1
)
ORDER BY rank
LIMIT 5;"
```
