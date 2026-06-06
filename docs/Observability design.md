# Observability Design

**Status:** Draft for team review  
**Target host:** Oracle Cloud VM.Standard.E5.Flex, 1 OCPU, 12 GB memory  
**Primary goal:** make every RAG answer debuggable from Discord event to final response.

## 1. Purpose And Scope

Observability should answer four questions:

- Did the bot route the event correctly?
- Did retrieval find useful context?
- Did the bot refuse when retrieval quality was too weak?
- Did context assembly preserve enough evidence without duplication?
- Did the final answer stay grounded in TPM Unite history?

In scope for v1:

- Active and passive Discord event tracing
- Qdrant retrieval tracing
- Rerank and dedupe tracing
- Context assembly tracing
- Gemini request/response tracing
- Low-quality retrieval refusal tracing
- Failed-match tracing
- Feedback/reaction correlation

Out of scope for v1:

- Full BI dashboarding
- Long-term prompt archive
- Automated LLM grading at production scale
- Multi-region or high-availability deployment

## 2. Hosting Assumptions

The current target instance is:

| Setting | Value |
|---|---|
| Shape | `VM.Standard.E5.Flex` |
| OCPU count | 1 |
| Memory | 12 GB |
| Network bandwidth | 1 Gbps |
| Local disk | Block storage only |

This is enough for the proposed v1 stack if we keep the services lean. The main constraint is CPU, not memory.

Important cost note: Oracle's Always Free compute resources are listed for `VM.Standard.E2.1.Micro` and Arm-based `VM.Standard.A1.Flex`. `VM.Standard.E5.Flex` is a flexible standard compute shape, so confirm billing/free-credit status before treating it as free.

Design constraints:

- Keep all services single-node.
- Prefer one Postgres instance for observability storage.
- Keep Phoenix internal or behind basic auth.
- Cap trace retention.
- Avoid storing full prompts forever.
- Prefer sampled full-prompt logging after launch.
- Use Qdrant only for vector memory, not operational logs.
- Watch CPU saturation during embedding, reranking, and Phoenix trace ingestion.

## 3. System Observability Goals

| Goal | What We Need To See |
|---|---|
| Routing quality | Active call, passive listener, ignored event, failed match |
| Retrieval quality | Query text, filters, retrieved chunks, scores, latency |
| Refusal quality | Whether weak, missing, or low-confidence retrieval correctly triggered refusal |
| Context quality | Reranked chunks, deduped chunks, dropped chunks, final context |
| Answer quality | Refusal, grounded response, citations, model latency |
| Feedback quality | Discord reactions or explicit feedback tied to one transaction |
| Operations | Error rates, slow nodes, Qdrant/Gemini failures, dispatch failures |

### Refusal Quality Metrics

Refusal is a core product behavior. The bot should refuse when retrieval is weak instead of producing an ungrounded answer.

| Metric | Meaning |
|---|---|
| `correct_refusal_rate` | No-context or weak-context cases that correctly refused |
| `false_refusal_rate` | Cases where useful context existed but the bot refused |
| `missed_refusal_rate` | Cases where context was weak but the bot answered |
| `low_score_refusal_count` | Refusals caused by retrieval or reranker thresholds |
| `post_dedupe_refusal_count` | Refusals caused by insufficient context after dedupe |
| `refusal_negative_feedback_rate` | Refused answers that received negative feedback |

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

For a 1 OCPU / 12 GB host, run lean:

| Service | Role | Guidance |
|---|---|---|
| n8n | Workflow execution | Keep workflows synchronous and small |
| Qdrant | Vector retrieval | One collection, local-only |
| Postgres | Phoenix + correlation tables | Small instance, daily backups |
| Phoenix | Trace UI and eval workflow | Internal access, short retention |

If CPU pressure appears, keep n8n/Qdrant/Postgres running and make Phoenix the first service to move off-host or run only when needed. If memory pressure appears, reduce trace retention before removing Phoenix.

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
| `refusal_reason` | Why the bot refused, if applicable |
| `created_at`, `completed_at` | Lifecycle timing |

Allowed `refusal_reason` values:

- `no_qdrant_results`
- `retrieval_score_below_threshold`
- `fewer_than_min_results`
- `reranker_score_below_threshold`
- `context_after_dedupe_insufficient`
- `context_token_budget_insufficient`
- `safety_or_policy_limit`

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

### `rag_weekly_metrics`

One precomputed row per reporting week. This prevents the weekly digest from requiring manual query assembly.

| Field | Purpose |
|---|---|
| `week_start`, `week_end` | Reporting window |
| `sample_size` | Number of evaluated bot interactions |
| `active_call_count` | User-invoked bot requests |
| `passive_candidate_count` | Passive events considered |
| `answered_count` | Bot responses sent with an answer |
| `refused_count` | Bot responses that refused |
| `context_found_rate` | Share of valid queries with usable retrieval context |
| `groundedness_pass_rate` | Evaluated answers that passed groundedness |
| `correct_refusal_rate` | Refusal-category cases that refused correctly |
| `thumbs_up_rate` | Positive reaction share |
| `rri` | `0.7 * groundedness_pass_rate + 0.3 * correct_refusal_rate` |
| `no_context_violation_count` | No-context cases where the bot answered anyway |
| `negative_feedback_count` | Explicit or reaction-based negative feedback |
| `p50_latency_ms`, `p95_latency_ms` | End-to-end latency |
| `generated_at` | When the rollup was produced |

## 6. Event And Span Taxonomy

Use stable names so traces are easy to filter.

| Stage | Event / Span |
|---|---|
| Ingress | `discord.event_received` |
| Routing | `routing.active_call`, `routing.passive_candidate`, `routing.ignored` |
| Retrieval | `qdrant.query_started`, `qdrant.query_completed`, `qdrant.no_context` |
| Retrieval quality | `retrieval.low_score`, `retrieval.fewer_than_min_results` |
| Rerank | `rerank.started`, `rerank.completed`, `rerank.low_confidence`, `rerank.failed` |
| Dedupe | `dedupe.started`, `dedupe.chunk_dropped`, `dedupe.completed` |
| Context | `context.assembled`, `context.overflow`, `context.insufficient`, `context.insufficient_after_dedupe` |
| LLM | `gemini.request_started`, `gemini.response_completed`, `gemini.failed` |
| Refusal | `response.refused`, `evaluation.correct_refusal`, `evaluation.false_refusal`, `evaluation.missed_refusal` |
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
4. **Retrieval gate node:** refuse if Qdrant results are missing or below threshold.
5. **Reranker node:** log candidate count, scores, selected chunks.
6. **Reranker gate node:** refuse if reranked context is not relevant enough.
7. **Dedupe node:** log kept/dropped chunks and reason.
8. **Context node:** log final context chunk IDs and token estimate.
9. **Context gate node:** refuse if context is insufficient after dedupe or token limits.
10. **Gemini node:** log model, latency, refusal/answer status.
11. **Discord node:** log response message ID or dispatch failure.
12. **Feedback workflow:** link reactions or feedback to transaction.

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
- Refusals by reason
- Refusals by channel
- False refusals and missed refusals from review/eval
- Answers with negative feedback
- Most reused chunks
- Dedupe drops by reason
- Slowest n8n nodes
- Weekly `#bot-metrics` digest

## 9. Weekly Metrics Digest

The evaluation design defines a weekly `#bot-metrics` digest. Observability owns making that digest easy to produce.

The digest should be generated from `rag_weekly_metrics`, not hand-assembled from multiple ad hoc queries.

Required weekly fields:

| Metric | Source |
|---|---|
| Sample size `n` | `rag_weekly_metrics.sample_size` |
| Context-found rate | Retrieval/result traces |
| Groundedness pass rate | Human or eval labels |
| Correct-refusal rate | Refusal eval labels |
| Thumbs-up % | Feedback/reaction correlation |
| RAG Reliability Index | Precomputed RRI formula |
| No-context violation count | Refusal/eval traces |
| Volume | Transaction counts |
| Latency | Transaction and stage timings |
| Top failure reasons | Refusal reasons, failed stages, negative feedback |

Publishing path:

1. A scheduled n8n workflow computes `rag_weekly_metrics`.
2. The workflow posts a concise digest to `#bot-metrics`.
3. The same row stays queryable in Postgres.
4. Phoenix links should be included only for notable failures or review samples.

The digest must show RRI with its components. RRI alone is not enough because a groundedness drop can be hidden by a refusal-rate increase.

## 10. Privacy, Retention, And Access

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

## 11. Implementation Plan

### Phase 1: Local Trace Foundation

- Add Postgres observability tables.
- Add Phoenix container with Postgres backend.
- Define transaction and span schemas.
- Log ingress, routing, retrieval, refusal, and failed-match events.

### Phase 2: n8n RAG Pipeline Instrumentation

- Add tracing to Qdrant query, rerank, dedupe, context assembly, Gemini, and Discord response nodes.
- Add the dedupe placeholder node after rerank and before context assembly.
- Log exact chunks retrieved and final chunks sent to Gemini.

### Phase 3: Feedback Correlation

- Capture reactions and explicit feedback.
- Link feedback to `discord_response_message_id`.
- Store feedback in Postgres and Phoenix traces.
- Define the missing feedback-correlation workflow: Discord reaction event, target bot message lookup, transaction lookup, feedback write, and unmatched-feedback handling.

### Phase 4: Weekly Metrics Rollup

- Add `rag_weekly_metrics`.
- Add a scheduled n8n rollup workflow.
- Publish the weekly `#bot-metrics` digest from the precomputed row.
- Include RRI, component rates, sample size, volume, latency, and top failure reasons.

### Phase 5: Evaluation Workflow

- Build a small evaluation set.
- Track groundedness, refusal correctness, and user feedback.
- Review false refusals and missed refusals as separate failure classes.
- Use Phoenix to inspect poor traces and create eval cases.

## 12. Open Questions

- Do we self-host Phoenix or use Phoenix Cloud later?
- What retention period is acceptable for full prompt/context text?
- Who can access Phoenix traces?
- Should `root_message_id` be required before final n8n dedupe?
- Feedback/reaction correlation is referenced in the architecture and evaluation docs, but the concrete implementation is still open. Which Discord events, message IDs, lookup tables, and failure paths should own this?
- What is the minimum dashboard we need before launch?
- Should passive listener traces sample more aggressively than active calls?

## References

- Oracle Always Free resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle service limits: https://docs.oracle.com/en-us/iaas/Content/General/service-limits/default.htm
- Phoenix self-hosting: https://arize.com/docs/phoenix/self-hosting/deploying-phoenix
- Phoenix configuration: https://arize.com/docs/phoenix/self-hosting/configuration
- Phoenix tracing: https://arize.com/docs/phoenix/learn/tracing/how-tracing-works
- n8n OpenTelemetry: https://docs.n8n.io/hosting/logging-monitoring/opentelemetry/
