# Observability Design

**Status:** Draft for team review  
**Target host:** Oracle Cloud VM.Standard.E5.Flex, 1 OCPU, 12 GB memory  
**Primary goal:** make every RAG answer debuggable from Discord event to final response.  
**Related:** Alerting design in `docs/Alerting.md`

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

Also keep a small set of explicit application-owned Postgres tables for durable transaction, feedback correlation, and weekly reporting. Phoenix is excellent for trace inspection and evaluation. Postgres is better for simple joins, retention controls, and operational rollups.

### Options

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| Phoenix + Postgres | Best RAG trace UI, supports LLM/retrieval debugging, durable storage | More services on 1 OCPU, needs retention discipline | Recommended |
| Phoenix + SQLite | Fastest local setup, fewer moving parts | Not ideal for shared/prod use, weaker durability | Dev only |
| Postgres-only | Lowest overhead, easy n8n writes, easy SQL | No AI trace UI, harder prompt/retrieval debugging | Fallback |
| Phoenix Cloud / Arize hosted | Lowest infra burden, strong UI | Data leaves our infra, possible cost/privacy concerns | Consider later |
| Qdrant for observability | Already running | Wrong storage model for logs and joins | Do not use |

### Runtime SLO And Concurrency Policy

The request path is mostly sequential, so fixed CPU allocation by service is not useful. The practical control is an end-to-end latency SLO, stage-level latency budgets, and a conservative concurrency policy.

Target for active bot calls:

- **p95 end-to-end latency:** less than 5 seconds

Starting stage budgets:

| Stage | Target | Notes |
|---|---|---|
| n8n routing and query prep | p95 < 100 ms | Basic workflow logic |
| Qdrant retrieval | p95 < 300 ms | Includes vector search and payload fetch |
| CrossEncoder rerank | p95 < 800 ms | Main local CPU-bound step |
| Dedupe and context assembly | p95 < 150 ms | Array/string logic should stay small |
| Observability writes | p95 < 200 ms | Keep synchronous payloads small |
| Gemini generation | p95 < 3,000 ms | External API latency dominates wall clock |
| Discord response dispatch | p95 < 300 ms | Includes outbound Discord call |

Concurrency policy:

- Start active-call workflow concurrency at 1.
- Queue or rate-limit concurrent active calls if p95 latency exceeds target.
- Rate-limit or sample passive-listener workflows.
- Do not run embedding/re-indexing jobs during serving windows.
- Keep observability writes small and avoid blocking the main response path where possible.
- If CPU pressure appears, reduce passive workload first, then reduce Phoenix trace payload size, then consider moving Phoenix off-host.

## 5. Transaction Trace Schema

Every event gets one `transaction_id`. Phoenix and Postgres both use this ID, but they serve different purposes:

- **Phoenix:** trace-level evidence for debugging and evaluation.
- **Postgres:** durable transaction, feedback, and weekly reporting records.

### 5a. Phoenix Transaction Trace Schema

Phoenix stores the step-by-step trace of a bot interaction. Each Phoenix trace should include one root span and child spans for routing, retrieval, reranking, dedupe, context assembly, LLM generation, response dispatch, and feedback.

Phoenix spans should be emitted progressively at major workflow checkpoints rather than only as one final trace dump. This gives maintainers partial trace evidence when a workflow fails mid-run, while avoiding an HTTP call after every tiny n8n node.

Starting checkpoint emission points:

| Checkpoint | Phoenix Spans |
|---|---|
| Start | `rag.active_call.started`, `discord.event_received`, `routing.active_call`, `query.normalized` |
| Embedding | `query.embedding_completed` |
| Retrieval and context | `qdrant.query_completed`, `qdrant.no_context`, `context.assembled`, `context.overflow`, `context.insufficient` |
| Gemini | `gemini.response_completed`, `gemini.failed` |
| Final dispatch | `rag.active_call`, `discord.response_sent`, `discord.response_failed` |

Postgres should not be used as the primary detailed node-level trace store. It should keep durable state and reporting inputs while Phoenix owns visual trace inspection.

Root trace attributes:

| Attribute | Purpose |
|---|---|
| `transaction_id` | Primary correlation key shared with Postgres |
| `discord_event_id` | Original Discord message/event ID |
| `route_type` | `active_call`, `passive_candidate`, `ignored` |
| `channel_id`, `channel_name` | Discord source |
| `query_hash` | Stable hash for grouping similar queries without exposing full text |
| `status` | `started`, `answered`, `refused`, `dropped`, `failed` |
| `refusal_reason` | Why the bot refused, if applicable |
| `failure_reason` | Why the workflow failed operationally, if applicable |

Child span groups:

| Span Group | Required Evidence |
|---|---|
| `routing.*` | Route decision and reason |
| `qdrant.*` | Query text/hash, filters, result count, latency |
| `rerank.*` | Candidate count, reranker scores, selected chunks |
| `dedupe.*` | Kept chunks, dropped chunks, dedupe reason |
| `context.*` | Final chunk IDs, token estimate, overflow handling |
| `gemini.*` | Model, prompt hash, response status, latency |
| `discord.*` | Response message ID or dispatch failure |
| `feedback.*` | Linked or unmatched feedback events |

Phoenix should store enough evidence for review, but not be the source of weekly reporting truth. It is acceptable to sample full prompt/context text after launch while keeping IDs and scores on every trace.

Context spans must make token-budget behavior explicit. If selected context exceeds the configured budget, n8n should drop the lowest-scored selected chunk until the context is under budget. If fewer than three chunks remain or the context is still over budget, the transaction should refuse with `context_token_budget_insufficient`.

Prompt hashes should be stable SHA-256 hashes, not base64 prefixes. `query_hash` and `prompt_hash` should both be safe to group on without exposing the original query or prompt text.

### 5b. Postgres Transaction Schema

Postgres stores durable records that n8n can query directly for correlation, dashboards, and the weekly digest.

#### `rag_transactions`

One row per Discord event considered by the bot.

| Field | Purpose |
|---|---|
| `transaction_id` | Primary correlation key |
| `discord_event_id` | Original Discord message/event ID |
| `route_type` | `active_call`, `passive_candidate`, `ignored` |
| `channel_id`, `channel_name` | Discord source |
| `author_id` | Requesting user |
| `user_query` | Cleaned query text |
| `query_hash` | SHA-256 hash of the normalized query for grouping without exposing query text |
| `status` | `started`, `answered`, `refused`, `dropped`, `failed` |
| `refusal_reason` | Why the bot refused, if applicable |
| `failure_reason` | Why the workflow failed operationally, if applicable |
| `created_at`, `completed_at` | Lifecycle timing |

Use `refusal_reason` only when the bot deliberately refuses because product quality gates say it should not answer. Use `failure_reason` when the workflow could not complete because of an operational issue, such as Gemini failure, Discord dispatch failure, Qdrant API failure, Postgres write failure, or malformed third-party response.

Allowed `refusal_reason` values:

- `no_qdrant_results`
- `retrieval_score_below_threshold`
- `fewer_than_min_results`
- `reranker_score_below_threshold`
- `context_after_dedupe_insufficient`
- `context_token_budget_insufficient`
- `safety_or_policy_limit`

Allowed starting `failure_reason` values:

- `query_embedding_failed`
- `qdrant_query_failed`
- `gemini_api_failed`
- `gemini_model_not_found`
- `gemini_auth_failed`
- `gemini_malformed_response`
- `discord_dispatch_failed`
- `postgres_write_failed`
- `workflow_timeout`

#### `rag_trace_events`

Append-only lifecycle events.

| Field | Purpose |
|---|---|
| `transaction_id` | Parent transaction |
| `stage` | Pipeline stage |
| `event_type` | Specific event name |
| `latency_ms` | Stage timing |
| `payload_json` | Structured details |
| `created_at` | Event timestamp |

Context-related payloads should include:

| Field | Purpose |
|---|---|
| `selected_context_count_before_budget` | Number of selected chunks before token-budget trimming |
| `selected_context_count` | Final number of chunks sent to Gemini |
| `context_token_budget` | Configured context-token budget |
| `context_token_estimate_before_budget` | Estimated context tokens before trimming |
| `context_token_estimate` | Final estimated context tokens |
| `context_dropped_for_budget_count` | Number of chunks dropped to stay under budget |
| `prompt_hash` | SHA-256 hash of the final prompt |

#### `rag_retrieval_results`

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

#### `rag_feedback`

One row per feedback signal.

Feedback is a satisfaction signal and review trigger. It should not be treated as an evaluation label by itself. For example, a negative reaction can flag a transaction for human review, but it does not automatically become `label = fail` in `rag_eval_labels`.

| Field | Purpose |
|---|---|
| `transaction_id` | Bot answer being evaluated |
| `discord_response_message_id` | Bot response message |
| `feedback_source` | Feedback channel, such as `reaction`, `context_menu`, `slash_command`, `form`, or `manual` |
| `feedback_type` | Legacy normalized feedback type: `positive`, `negative`, or `explicit` |
| `feedback_value` | Normalized sentiment or structured value, such as `positive`, `negative`, a reaction name, or a form value |
| `reaction_name` | Raw Discord reaction name when `feedback_source = reaction` |
| `feedback_text` | Optional explicit critique text |
| `feedback_category` | Optional failure category, such as `made_something_up`, `did_not_answer`, `wrong_tone`, `surfaced_personal_info`, or `other` |
| `matched` | Whether the feedback was linked to a known bot transaction |
| `review_candidate` | True when the feedback should be reviewed by a human |
| `review_status` | `pending`, `in_review`, `resolved`, or `dismissed` |
| `feedback_author_id_hash` | Hashed feedback source |
| `created_at` | Feedback timestamp |

Feedback write rule:

- Positive reaction or praise: write `feedback_type = positive`; `review_candidate = false`.
- Negative reaction or explicit critique: write `feedback_type = negative` or `explicit`; set `review_candidate = true` and `review_status = pending`.
- Unmatched feedback: write `matched = false` when a transaction cannot be linked; do not include it in weekly quality metrics until linked.

This keeps feedback separate from evaluation labels. Human or judge review can later convert a review candidate into rows in `rag_eval_labels`.

#### `rag_eval_labels`

One row per evaluation label. This is the source of truth for weekly quality rollups.

Phoenix can display annotations and review context, but weekly metrics should read from this Postgres table so the digest is stable and queryable.

| Field | Purpose |
|---|---|
| `transaction_id` | Bot interaction being evaluated |
| `dimension` | `groundedness`, `answer_relevance`, `tone_refusal`, `safety` |
| `label` | `pass` or `fail` |
| `failure_type` | Optional failure category, such as `unsupported_claim`, `correct_refusal`, `false_refusal`, `missed_refusal`, `no_context_violation`, or `pii` |
| `source` | `human` or `judge` |
| `labeler` | Human reviewer or judge identifier |
| `notes` | Optional short review note |
| `created_at` | Label timestamp |

Refusal outcome vocabulary:

- `correct_refusal`
- `false_refusal`
- `missed_refusal`
- `no_context_violation`

These values are stored in `failure_type` on the `tone_refusal` dimension. `no_context_violation` is the non-negotiable launch gate: no-context cases must not produce ungrounded answers.

#### `rag_weekly_metrics`

One precomputed row per reporting week in the application-owned Postgres schema. This prevents the weekly digest from requiring manual query assembly.

Phoenix does not own this rollup. Phoenix stores traces and review context that explain why a metric moved. Postgres stores eval labels and the weekly metric row that n8n can publish and query directly.

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
| `rag_reliability_index` | `0.7 * groundedness_pass_rate + 0.3 * correct_refusal_rate` |
| `no_context_violation_count` | No-context cases where the bot answered anyway |
| `negative_feedback_count` | Explicit or reaction-based negative feedback |
| `p50_latency_ms`, `p95_latency_ms` | End-to-end latency |
| `generated_at` | When the rollup was produced |

## 6. Event And Span Taxonomy

Use stable names so traces are easy to filter.

These are logical observability events, not native events emitted directly by Discord, Qdrant, or Gemini. n8n should emit these events as the workflow runs.

- Phoenix stores them as trace spans/events for visual debugging.
- Postgres stores key events as durable rows in `rag_trace_events` for reporting and weekly metrics.
- For active-call execution traces, prefer progressive Phoenix checkpoint emission over a single end-of-workflow trace upload.

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
| Refusal | `response.refused`, `evaluation.correct_refusal`, `evaluation.false_refusal`, `evaluation.missed_refusal`, `evaluation.no_context_violation` |
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
- `failure_reason`, when applicable

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
9. **Context gate node:** enforce the context-token budget by dropping lowest-scored chunks, log `context.overflow` when trimming occurs, and refuse with `context_token_budget_insufficient` if fewer than three chunks remain or the context is still over budget.
10. **Gemini node:** log model, latency, refusal/answer status.
11. **Discord node:** log response message ID or dispatch failure.
12. **Feedback workflow:** link reactions or feedback to transaction.

Implementation note:

- Use n8n OpenTelemetry for workflow/node execution traces where practical.
- Use explicit Postgres writes for RAG-specific events that need durable querying.
- Send custom Phoenix spans progressively at major workflow checkpoints: start, embedding, retrieval/context, Gemini, and final dispatch.
- Use the internal trace-emitter service when n8n emits JSON checkpoint payloads. The trace emitter converts JSON payloads to OTLP protobuf before forwarding them to Phoenix.
- Keep Postgres writes on the hot path limited to durable transaction state and reporting inputs, such as retrieval result rows.

## 8. User Interaction With Observability

| User | What They Need |
|---|---|
| Maintainer | See failed transactions, slow nodes, service errors |
| Developer | Inspect retrieval candidates, scores, dedupe, final context |
| Reviewer | Check whether an answer was grounded and cited correctly |
| Community lead | Understand feedback trends and recurring failure modes |

### Minimum Useful Views And Access Paths

Views should be created where the data is easiest to query. Phoenix is best for trace inspection. Postgres is best for durable reporting views. n8n is best for scheduled summaries and Discord posts.

| View | Source | Access Path |
|---|---|---|
| Failed context matches | Phoenix traces + `rag_trace_events` | Phoenix UI filter, Postgres SQL view |
| Low-score retrievals | `rag_retrieval_results` + Phoenix retrieval spans | Postgres SQL view, Phoenix trace links |
| Refusals by reason | `rag_transactions.refusal_reason` | Postgres SQL view, weekly digest |
| Refusals by channel | `rag_transactions` | Postgres SQL view, weekly digest |
| False refusals and missed refusals | Evaluation labels + Phoenix traces | Phoenix eval view, Postgres reporting view |
| Answers with negative feedback | `rag_feedback` | Postgres SQL view with Phoenix trace links |
| Most reused chunks | `rag_retrieval_results.chunk_id` | Postgres SQL view |
| Dedupe drops by reason | `rag_retrieval_results.dedupe_reason` + Phoenix dedupe spans | Postgres SQL view, Phoenix trace links |
| Slowest n8n nodes | n8n/OpenTelemetry spans | Phoenix trace view |
| Weekly `#bot-metrics` digest | `rag_weekly_metrics` | Scheduled n8n post to Discord |

Access expectations:

- Maintainers use the weekly Discord digest first.
- Developers use Phoenix links to inspect individual failed or surprising traces.
- Reviewers use Phoenix eval views and Postgres reporting views for groundedness/refusal review.
- Postgres SQL views should exist for repeat reporting so weekly metrics do not require ad hoc query assembly.

## 9. Weekly Metrics Digest

The evaluation design defines a weekly `#bot-metrics` digest. Observability owns making that digest easy to produce.

The digest should be generated from Postgres table `rag_weekly_metrics`, not hand-assembled from multiple ad hoc queries.

Phoenix's role:

- Store trace-level evidence.
- Display annotations and review context where useful.
- Let reviewers inspect examples behind each metric.
- Link notable failures from the weekly digest back to traces.

Postgres's role:

- Store transaction and feedback correlation records.
- Store eval labels in `rag_eval_labels`.
- Store the precomputed weekly rollup.
- Provide a stable source for the scheduled digest.
- Support simple SQL reporting without depending on Phoenix UI exports.

Required weekly fields:

| Metric | Source |
|---|---|
| Sample size `n` | `rag_weekly_metrics.sample_size` |
| Context-found rate | Retrieval/result traces |
| Groundedness pass rate | `rag_eval_labels` |
| Correct-refusal rate | `rag_eval_labels` |
| Thumbs-up % | Feedback/reaction correlation |
| RAG Reliability Index | Precomputed RRI formula |
| No-context violation count | `rag_eval_labels` + refusal traces |
| Volume | Transaction counts |
| Latency | Transaction and stage timings |
| Top failure reasons | Refusal reasons, failed stages, negative feedback |

Publishing path:

1. A scheduled n8n workflow reads Postgres transaction, feedback, retrieval, and eval-label rows.
2. The workflow computes and upserts `rag_weekly_metrics`.
3. The workflow posts a concise digest to `#bot-metrics`.
4. The same row stays queryable in Postgres.
5. Phoenix links should be included only for notable failures or review samples.

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
- Write `feedback_source`, `feedback_type`, and `feedback_value` separately so the source channel is not confused with sentiment.
- For negative reactions or explicit critique, set `review_candidate = true` and `review_status = pending`.
- Use `matched = false` for feedback that cannot be linked to a known bot transaction.
- Define the missing feedback-correlation workflow: Discord reaction event, target bot message lookup, transaction lookup, feedback write, and unmatched-feedback handling.

### Phase 4: Weekly Metrics Rollup

- Add `rag_weekly_metrics`.
- Add a scheduled n8n rollup workflow.
- Publish the weekly `#bot-metrics` digest from the precomputed row.
- Include RRI, component rates, sample size, volume, latency, and top failure reasons.

### Phase 5: Evaluation Workflow

- Build a small evaluation set.
- Add `rag_eval_labels` as the source of truth for pass/fail labels.
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
