# Observability Design

**Status:** Draft for team review  
**Target host:** Oracle Cloud Free Tier, 1 OCPU operating assumption  
**Primary goal:** make every RAG answer debuggable from Discord event to final response.

## 1. Purpose And Scope

Observability should answer four questions:

- Did the bot route the event correctly?
- Did retrieval find useful context?
- Did context assembly preserve enough evidence without duplication?
- Did the final answer stay grounded in TPM Unite history?

In scope for v1:

- Active and passive Discord event tracing
- Qdrant retrieval tracing
- Rerank and dedupe tracing
- Context assembly tracing
- Gemini request/response tracing
- Refusal and failed-match tracing
- Feedback/reaction correlation

Out of scope for v1:

- Full BI dashboarding
- Long-term prompt archive
- Automated LLM grading at production scale
- Multi-region or high-availability deployment

## 2. Hosting Assumptions

Oracle Always Free offers two relevant compute paths:

- **VM.Standard.A1.Flex:** up to 4 OCPUs and 24 GB memory across the tenancy.
- **VM.Standard.E2.1.Micro:** 1/8 OCPU and 1 GB memory.

This design assumes we run on **1 OCPU A1.Flex**, not E2.1.Micro. The micro shape is too small for n8n, Qdrant, Phoenix, and Postgres together.

Design constraints:

- Keep all services single-node.
- Prefer one Postgres instance for observability storage.
- Keep Phoenix internal or behind basic auth.
- Cap trace retention.
- Avoid storing full prompts forever.
- Prefer sampled full-prompt logging after launch.
- Use Qdrant only for vector memory, not operational logs.

## 3. System Observability Goals

| Goal | What We Need To See |
|---|---|
| Routing quality | Active call, passive listener, ignored event, failed match |
| Retrieval quality | Query text, filters, retrieved chunks, scores, latency |
| Context quality | Reranked chunks, deduped chunks, dropped chunks, final context |
| Answer quality | Refusal, grounded response, citations, model latency |
| Feedback quality | Discord reactions or explicit feedback tied to one transaction |
| Operations | Error rates, slow nodes, Qdrant/Gemini failures, dispatch failures |

## 4. Tech Stack Recommendation

Recommended v1 stack:

```text
Discord
  -> n8n
      -> Qdrant
      -> Phoenix
      -> Postgres
      -> Gemini
```

### Recommended Choice

Use **Phoenix self-hosted with Postgres** as the AI trace layer.

Also keep a small set of explicit Postgres tables for durable transaction and feedback correlation. Phoenix is excellent for trace inspection; Postgres is better for simple joins, retention controls, and operational reporting.

### Options

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| Phoenix + Postgres | Best RAG trace UI, supports LLM/retrieval debugging, durable storage | More services on 1 OCPU, needs retention discipline | Recommended |
| Phoenix + SQLite | Fastest local setup, fewer moving parts | Not ideal for shared/prod use, weaker durability | Dev only |
| Postgres-only | Lowest overhead, easy n8n writes, easy SQL | No AI trace UI, harder prompt/retrieval debugging | Fallback |
| Phoenix Cloud / Arize hosted | Lowest infra burden, strong UI | Data leaves our infra, possible cost/privacy concerns | Consider later |
| Qdrant for observability | Already running | Wrong storage model for logs and joins | Do not use |

### Resource Budget

For a 1 OCPU host, run lean:

| Service | Role | Guidance |
|---|---|---|
| n8n | Workflow execution | Keep workflows synchronous and small |
| Qdrant | Vector retrieval | One collection, local-only |
| Postgres | Phoenix + correlation tables | Small instance, daily backups |
| Phoenix | Trace UI and eval workflow | Internal access, short retention |

If CPU or memory pressure appears, keep n8n/Qdrant/Postgres running and make Phoenix the first service to move off-host or run only when needed.

## 5. Transaction Trace Schema

Every event gets one `transaction_id`.

### `rag_transactions`

One row per Discord event considered by the bot.

| Field | Purpose |
|---|---|
| `transaction_id` | Primary correlation key |
| `discord_event_id` | Original Discord message/event ID |
| `route_type` | `active_call`, `passive_candidate`, `ignored` |
| `channel_id`, `channel_name` | Discord source |
| `author_id` | Requesting user |
| `user_query` | Cleaned query text |
| `status` | `started`, `answered`, `refused`, `dropped`, `failed` |
| `created_at`, `completed_at` | Lifecycle timing |

### `rag_trace_events`

Append-only lifecycle events.

| Field | Purpose |
|---|---|
| `transaction_id` | Parent transaction |
| `stage` | Pipeline stage |
| `event_type` | Specific event name |
| `latency_ms` | Stage timing |
| `payload_json` | Structured details |
| `created_at` | Event timestamp |

### `rag_retrieval_results`

One row per retrieved chunk candidate.

| Field | Purpose |
|---|---|
| `transaction_id` | Parent transaction |
| `chunk_id` | Qdrant point ID |
| `channel_id`, `channel_name` | Source location |
| `message_ids` | Source messages |
| `first_message_id` | Discord link target |
| `root_message_id` | Future dedupe signal |
| `retrieval_score` | Qdrant cosine score |
| `reranker_score` | CrossEncoder score |
| `rank_before`, `rank_after` | Retrieval/rerank order |
| `dedupe_status` | `kept`, `dropped`, `not_applied` |
| `dedupe_reason` | `overlap`, `same_root`, `low_score`, etc. |

### `rag_feedback`

One row per feedback signal.

| Field | Purpose |
|---|---|
| `transaction_id` | Bot answer being evaluated |
| `discord_response_message_id` | Bot response message |
| `feedback_type` | `reaction`, `slash_command`, `form` |
| `feedback_value` | Positive, negative, or structured value |
| `feedback_author_id` | Feedback source |
| `created_at` | Feedback timestamp |

## 6. Event And Span Taxonomy

Use stable names so traces are easy to filter.

| Stage | Event / Span |
|---|---|
| Ingress | `discord.event_received` |
| Routing | `routing.active_call`, `routing.passive_candidate`, `routing.ignored` |
| Retrieval | `qdrant.query_started`, `qdrant.query_completed`, `qdrant.no_context` |
| Rerank | `rerank.started`, `rerank.completed`, `rerank.failed` |
| Dedupe | `dedupe.started`, `dedupe.chunk_dropped`, `dedupe.completed` |
| Context | `context.assembled`, `context.overflow`, `context.insufficient` |
| LLM | `gemini.request_started`, `gemini.response_completed`, `gemini.failed` |
| Response | `discord.response_sent`, `discord.response_failed` |
| Feedback | `feedback.received`, `feedback.linked`, `feedback.unmatched` |

Required attributes:

- `transaction_id`
- `route_type`
- `channel_id`
- `query_hash`
- `status`
- `latency_ms`
- `error_type`, when applicable

## 7. n8n Integration Points

n8n remains the orchestration and context assembly layer.

Instrumentation points:

1. **Ingress node:** create `transaction_id`; log raw event summary.
2. **Routing node:** log active/passive/ignored decision.
3. **Qdrant node:** log query, filters, latency, raw candidates.
4. **Reranker node:** log candidate count, scores, selected chunks.
5. **Dedupe node:** log kept/dropped chunks and reason.
6. **Context node:** log final context chunk IDs and token estimate.
7. **Gemini node:** log model, latency, refusal/answer status.
8. **Discord node:** log response message ID or dispatch failure.
9. **Feedback workflow:** link reactions or feedback to transaction.

Implementation note:

- Use n8n OpenTelemetry for workflow/node execution traces where practical.
- Use explicit Postgres writes for RAG-specific events that need durable querying.
- Send custom Phoenix spans for retrieval, rerank, dedupe, context, and LLM calls.

## 8. User Interaction With Observability

| User | What They Need |
|---|---|
| Maintainer | See failed transactions, slow nodes, service errors |
| Developer | Inspect retrieval candidates, scores, dedupe, final context |
| Reviewer | Check whether an answer was grounded and cited correctly |
| Community lead | Understand feedback trends and recurring failure modes |

Minimum useful views:

- Failed context matches
- Low-score retrievals
- Refusals by channel
- Answers with negative feedback
- Most reused chunks
- Dedupe drops by reason
- Slowest n8n nodes

## 9. Privacy, Retention, And Access

Discord history is community data. Treat traces as sensitive.

Guidelines:

- Store IDs and metadata by default.
- Store full prompts only for short retention or sampled debugging.
- Redact bot tokens, API keys, and secrets.
- Avoid storing unnecessary user PII.
- Keep Phoenix private or behind authentication.
- Keep operational logs for 30 days by default.
- Keep feedback and aggregate metrics longer.

Suggested retention:

| Data | Retention |
|---|---|
| Full prompt/context traces | 14-30 days |
| Transaction metadata | 90 days |
| Feedback records | 1 year |
| Aggregated metrics | Indefinite |

## 10. Implementation Plan

### Phase 1: Local Trace Foundation

- Add Postgres observability tables.
- Add Phoenix container with Postgres backend.
- Define transaction and span schemas.
- Log ingress, routing, retrieval, and failed-match events.

### Phase 2: n8n RAG Pipeline Instrumentation

- Add tracing to Qdrant query, rerank, dedupe, context assembly, Gemini, and Discord response nodes.
- Add the dedupe placeholder node after rerank and before context assembly.
- Log exact chunks retrieved and final chunks sent to Gemini.

### Phase 3: Feedback Correlation

- Capture reactions and explicit feedback.
- Link feedback to `discord_response_message_id`.
- Store feedback in Postgres and Phoenix traces.

### Phase 4: Evaluation Workflow

- Build a small evaluation set.
- Track groundedness, refusal correctness, and user feedback.
- Use Phoenix to inspect poor traces and create eval cases.

## 11. Open Questions

- Do we self-host Phoenix or use Phoenix Cloud later?
- What retention period is acceptable for full prompt/context text?
- Who can access Phoenix traces?
- Should `root_message_id` be required before final n8n dedupe?
- What is the minimum dashboard we need before launch?
- Should passive listener traces sample more aggressively than active calls?

## References

- Oracle Always Free resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle service limits: https://docs.oracle.com/en-us/iaas/Content/General/service-limits/default.htm
- Phoenix self-hosting: https://arize.com/docs/phoenix/self-hosting/deploying-phoenix
- Phoenix configuration: https://arize.com/docs/phoenix/self-hosting/configuration
- Phoenix tracing: https://arize.com/docs/phoenix/learn/tracing/how-tracing-works
- n8n OpenTelemetry: https://docs.n8n.io/hosting/logging-monitoring/opentelemetry/
