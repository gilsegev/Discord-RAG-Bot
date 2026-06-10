# Phase 3B: Phoenix Trace Implementation
**Status:** Draft
**Scope:** Correct Phase 3 observability so Phoenix owns visual trace inspection and Postgres owns durable state/reporting.
**Related:** Phase 3 n8n Implementation, Observability Design, Architecture Overview, Server Setup Hardening

## Purpose
Phase 3 proved that the active-call workflow can produce durable observability evidence, but it wrote detailed node-level trace events into Postgres.

That is only half of the observability design.

The intended split is:

| Layer | Responsibility |
|---|---|
| Phoenix | Visual trace inspection, span hierarchy, latency breakdown, retrieval/context/prompt debugging |
| Postgres | Durable transaction state, retrieval result rows, feedback/eval/reporting tables, weekly metrics |

Phase 3B moves detailed per-node trace evidence to Phoenix while keeping Postgres for durable bot state.

Phoenix traces are emitted progressively at major checkpoints rather than only once at the end. This gives us partial trace evidence when a workflow fails midway without adding a Phoenix write after every tiny n8n node.

## What Changes
Phase 3B keeps the working active-call path:

```text
manual active call
-> create transaction
-> normalize query
-> embed query
-> query Qdrant
-> apply retrieval threshold
-> assemble context
-> call Gemini or refuse
-> post Discord response
-> finalize transaction
```

But changes the observability path:

```text
Phase 3:
n8n -> Postgres rag_trace_events for detailed node trace events

Phase 3B:
n8n -> Phoenix OTLP trace spans at major checkpoints
n8n -> Postgres only for durable state and reporting inputs
```

## Workflow Artifact
Import this file into repo-owned n8n:

```text
workflows/n8n/rag-active-call-phase-3b-phoenix-tracing.json
```

Workflow name:

```text
RAG Active Call - Phase 3B Phoenix Tracing
```

## Hardened Service Names
This workflow uses the repo-owned n8n service-name setup from the server hardening work.

| Service | URL / Host |
|---|---|
| Postgres credential host | `postgres` |
| Qdrant | `http://qdrant:6333` |
| Embedder | `http://embedder:8000/embed` |
| Trace emitter | `http://trace-emitter:8001/v1/traces` |
| Phoenix OTLP HTTP collector | `http://phoenix:6006/v1/traces` |

Do not use container IPs such as `172.20.x.x` in this workflow.

The workflow sends JSON trace payloads to the internal trace emitter. The trace emitter converts those payloads to OTLP protobuf and forwards them to Phoenix. This adapter exists because n8n HTTP nodes are convenient for JSON, while Phoenix's OTLP HTTP collector expects protobuf.

## Postgres Responsibilities
Keep these Postgres writes:

- Create and update `rag_transactions`
- Store final transaction state
- Store `rag_retrieval_results` for evaluation/reporting

Do not use Postgres as the primary visual trace layer.

Phase 3B removes detailed hot-path writes to:

```text
rag_trace_events
```

from the n8n workflow.

Postgres may still keep compact final summaries later if needed for weekly metrics, but detailed node spans belong in Phoenix.

## Phoenix Responsibilities
Phoenix receives OTLP/HTTP trace payloads at major checkpoints during the workflow through the internal trace emitter service:

```text
n8n
  -> trace-emitter:8001/v1/traces
      -> phoenix:6006/v1/traces
```

The checkpoint emissions are:

| Checkpoint | Node Pair | Spans |
|---|---|---|
| Start | `Build Phoenix Start Checkpoint` -> `Send Phoenix Start Checkpoint` | `rag.active_call.started`, `discord.event_received`, `routing.active_call`, `query.normalized` |
| Embedding | `Build Phoenix Embedding Checkpoint` -> `Send Phoenix Embedding Checkpoint` | `query.embedding_completed` |
| Retrieval/context | `Build Phoenix Retrieval Context Checkpoint` -> `Send Phoenix Retrieval Context Checkpoint` | `qdrant.query_completed`, `context.assembled`, `context.overflow`, or `context.insufficient` |
| Gemini | `Build Phoenix Gemini Checkpoint` -> `Send Phoenix Gemini Checkpoint` | `gemini.response_completed` or `gemini.failed` |
| Final | `Build Phoenix Final Checkpoint` -> `Send Phoenix Final Checkpoint` | `rag.active_call`, `discord.response_sent`, or `discord.response_failed` |

The resulting trace contains:

- one root span: `rag.active_call`
- child spans:
  - `discord.event_received`
  - `query.normalized`
  - `query.embedding_completed`
  - `qdrant.query_completed`
  - `context.assembled`, `context.overflow`, or `context.insufficient`
  - `gemini.response_completed` or `gemini.failed`
  - `discord.response_sent` or `discord.response_failed`

Each span includes the relevant IDs, status, latency, refusal/failure reason, score counts, token estimates, and hash fields.

## What Stays The Same From Phase 3
- Active-call happy path is still manual-trigger based for testing.
- Gemini still uses `gemini-3.5-flash`.
- Query embedding still uses local Nomic Embed v1.5 through the embedder service.
- Qdrant still searches `top_k = 20`.
- Retrieval score threshold remains `0.55`.
- Reranking and dedupe are still intentionally excluded until the next phase.
- Context-budget trimming remains a temporary safety rail, not the final context-quality design.

## Values To Replace In n8n
Update the `Set Manual Active Call` node before running:

```text
gemini_url = https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key=<your key>
discord_webhook_url = <your Discord webhook URL>?wait=true
```

These values should stay in the n8n runtime only. Do not commit them to Git.

## Expected Success
For an answered run:

```text
Postgres:
  rag_transactions.status = answered
  rag_transactions.retrieval_status = context_found
  rag_transactions.response_status = posted

Phoenix:
  trace exists for the transaction
  root span = rag.active_call
  child spans include qdrant, context, gemini, and discord
```

For a refusal run:

```text
Postgres:
  rag_transactions.status = refused
  rag_transactions.retrieval_status = no_context
  rag_transactions.response_status = posted
  rag_transactions.refusal_reason is populated

Phoenix:
  trace exists for the transaction
  context span shows context.insufficient or qdrant span shows no usable retrieval
```

For an operational failure:

```text
Postgres:
  rag_transactions.status = failed
  rag_transactions.failure_reason is populated

Phoenix:
  failed span is marked with status code error
```

## Validation
Open Phoenix through an SSH tunnel:

```powershell
ssh -i "$HOME\mykey.key" -L 6006:127.0.0.1:6006 ubuntu@discord-notifier.duckdns.org
```

Then open:

```text
http://127.0.0.1:6006
```

Look for the `discord-rag-bot-phase-3b` project or traces with:

```text
transaction_id
query_hash
rag.active_call
```

Validate durable state in Postgres:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT transaction_id, status, retrieval_status, response_status,
       refusal_reason, failure_reason, discord_response_message_id,
       query_hash, latency_ms, created_at
FROM rag_transactions
ORDER BY created_at DESC
LIMIT 5;"
```

Validate retrieval rows:

```bash
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT rank, retrieval_score, channel_name, first_message_id,
       left(payload->>'text', 200) AS text_preview
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

## Known Risks
- Phoenix OTLP/HTTP ingestion must be verified against the running Phoenix container through the trace emitter.
- If Phoenix rejects the trace payload, the workflow should still finalize Postgres state.
- n8n is not a full tracing SDK, so this workflow manually builds an OTLP JSON payload and the trace emitter converts it to OTLP protobuf.
- Rerank/dedupe are still missing, so context quality is not final.

## References
- Phoenix tracing setup: https://arize.com/docs/phoenix/tracing/how-to-tracing/setup-tracing/setup-using-phoenix-otel
- Phoenix self-hosted configuration: https://arize.com/docs/phoenix/self-hosting/configuration
