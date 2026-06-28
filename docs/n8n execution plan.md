# n8n Execution Plan
**Status:** Draft for implementation planning
**Scope:** Methodical rollout plan for building the n8n RAG workflow
**Related:** n8n Workflow Design, Observability Design, Alerting, Retrieval Context Prompt Contracts

## Purpose
This document defines the recommended implementation order for the n8n workflow.

The goal is to avoid building the entire system at once, then spending days debugging a large workflow with unclear failure points.

The implementation should start with a narrow, observable happy path, then expand one capability at a time.

## Guiding Principle
Do not build the full RAG bot in one pass.

Build the smallest useful active-call workflow first, make it observable, then add gates, branches, reranking, dedupe, passive listening, feedback, metrics, and alerts incrementally.

Each step should answer one question:

- Can we receive?
- Can we log?
- Can we retrieve?
- Can we refuse?
- Can we answer?
- Can we explain what happened?

## Phase 0: Runtime Foundation
Set up the infrastructure before implementing RAG logic.

What to stand up:

- n8n
- Postgres
- Phoenix
- Qdrant

Validation:

- n8n can write a test row to Postgres.
- n8n can write a test trace/span to Phoenix.
- n8n can reach Qdrant locally.
- n8n can make an outbound test call to Discord or a webhook.

Expected outcome:

The services can talk to each other before any retrieval or LLM logic is added.

## Phase 1: Minimum Transaction Spine
Create the minimum durable transaction model in Postgres.

Minimum fields:

- `transaction_id`
- incoming Discord message ID
- Discord channel ID
- Discord author ID or hashed author ID
- incoming message timestamp
- route type
- transaction status
- retrieval status
- response status
- refusal reason
- created timestamp
- completed timestamp

Expected outcome:

Every workflow run has one durable transaction row that can be inspected outside n8n.

Implementation artifact:

```text
workflows/n8n/rag-active-call-phase-1-transaction-spine.json
```

Implementation notes:

- This workflow simulates an active call with a manual trigger.
- It writes transaction and trace rows to Postgres.
- It checks whether the Qdrant collection exists.
- It does not perform real embedding, vector search, Gemini generation, or Discord dispatch.

## Phase 2: One Active-Call Happy Path
Build only the direct bot mention path first.

Do not implement passive listener behavior yet.

Happy path:

```text
Discord mention
-> create transaction
-> normalize query
-> embed query
-> query Qdrant
-> apply simple retrieval threshold
-> assemble context
-> call Gemini
-> post Discord response
-> finalize transaction
```

Temporarily exclude:

- passive listener
- reranker
- dedupe
- reaction boost
- feedback correlation
- weekly metrics
- alert routing beyond basic failure logging

Expected outcome:

One known question can produce one grounded response or one refusal, with a transaction row and trace evidence.

Implementation artifact for the first Phase 2 gate:

```text
workflows/n8n/rag-active-call-phase-2-retrieval-gate.json
```

Implementation notes:

- This workflow checks whether Qdrant has the target collection and whether the collection has points.
- It does not yet embed the query or execute vector search.
- It fails/refuses cleanly when retrieval prerequisites are missing.
- It marks the transaction as ready for embedding and vector search when Qdrant is populated.

Implementation artifact for the full Phase 2 active-call path:

```text
workflows/n8n/rag-active-call-phase-2-full-happy-path.json
```

Implementation notes:

- This workflow performs query embedding, Qdrant vector search, simple retrieval thresholding, context assembly, Gemini generation, Discord posting, and final transaction logging.
- It requires a query embedding service, Gemini API key, and Discord webhook before it can execute end to end.
- It still excludes passive listener behavior, reranking, dedupe, reaction boost, feedback correlation, weekly metrics, and advanced alerting.

## Phase 3: Node-Level Observability
Instrument each active-call node.

For every major node, log:

- node started
- node completed or failed
- latency
- key input summary
- key output summary
- decision made
- error reason if failed
- `failure_reason` when the workflow fails operationally
- SHA-256 `query_hash` and SHA-256 `prompt_hash` when query/prompt grouping is needed
- context token-budget fields when context is assembled or trimmed

Phoenix should show the execution trace.

Postgres should store durable transaction state and key events.

Expected outcome:

When the workflow fails, the failure point is obvious without manually stepping through the entire n8n canvas.

Implementation artifact:

```text
workflows/n8n/rag-active-call-phase-3-node-observability.json
```

Implementation notes:

- This workflow keeps the Phase 2 active-call happy path and adds durable trace events for each major node.
- It records stage-level latency, key input/output summaries, routing/retrieval/generation/dispatch decisions, and failure reasons.
- It records Gemini API failures as operational failures instead of retrieval refusals.
- It separates `refusal_reason` from `failure_reason`: refusal is a product quality decision, failure is an operational execution problem.
- It enforces the context-token budget before Gemini. If selected context is too large, it drops the lowest-scored chunks until under budget. If fewer than three chunks remain, it refuses with `context_token_budget_insufficient`.
- It logs `context.overflow` when context had to be trimmed and stores before/after token estimates.
- It still excludes passive listener behavior, reranking, dedupe, reaction boost, feedback correlation, weekly metrics, and advanced alerting.

## Phase 4: Stage 1 Retrieval Refusal Gate
Harden the Qdrant-stage refusal logic before adding reranking or dedupe.

This phase only owns the Stage 1 gate from the retrieval contract. It does not decide reranker refusal, dedupe sufficiency, or final LLM grounding refusal.

Gate:

```text
Did Qdrant find usable context?
```

If no:

- record failed retrieval
- set refusal reason
- return the standard refusal response
- finalize the transaction

If yes:

- continue to context assembly and generation

Expected outcome:

The bot refuses when Qdrant cannot provide at least three candidates above `retrieval_score >= 0.55`, and the reason is explicit in Postgres and Phoenix.

Phase 4 intentionally stops short of the full retrieval contract:

- reranker refusal is added in Phase 5
- dedupe-driven context sufficiency is added in Phase 6
- exact context block formatting and final prompt refusal are finalized in Phase 7

Implementation artifact:

```text
workflows/n8n/rag-active-call-phase-4-stage-1-retrieval-gate.json
```

Implementation notes:

- Adds `Build Stage 1 Retrieval Gate` immediately after Qdrant search.
- Records `stage_1_gate_status`, `stage_1_gate_reason`, threshold, raw candidate count, and passed candidate count.
- Emits a Phoenix span named `retrieval.stage1_gate_passed` or `retrieval.stage1_gate_refused`.
- Keeps the Phase 3B Phoenix trace emitter path and durable Postgres transaction state.

## Phase 5: Reranker
Add the CrossEncoder reranker after raw Qdrant retrieval is working.

Flow:

```text
Qdrant top-k
-> rerank candidates
-> apply reranker quality gate
```

Validation:

- Compare Qdrant-only results against reranked results for known questions.
- Record both `retrieval_score` and `reranker_score`.
- Confirm weak reranker results trigger refusal.

Expected outcome:

The workflow improves relevance without changing the rest of the active-call path.

Implementation artifact:

```text
workflows/n8n/rag-active-call-phase-5-reranker.json
```

Implementation notes:

- Adds repo-owned reranker service at `http://reranker:8002/rerank`.
- Uses `cross-encoder/ms-marco-MiniLM-L-6-v2`.
- Refuses before Gemini when fewer than three candidates have `reranker_score > 0`.
- Stores both `retrieval_score` and `reranker_score`.
- Emits Phoenix rerank spans.

## Phase 6: Dedupe Placeholder
Add message-overlap dedupe after reranking and before context assembly.

Detailed design and implementation readiness review:

```text
docs/Phase 6.md
```

Phase 6 currently depends on the Phase 5 reranker merge and the per-piece `message_ids` ingestion correction. Do not validate dedupe quality against split chunks until Qdrant has been rebuilt with corrected payloads.

Initial rule:

```text
shared = intersection(chunk_a.message_ids, chunk_b.message_ids)
overlap_ratio = len(shared) / min(len(chunk_a.message_ids), len(chunk_b.message_ids))
```

If `overlap_ratio > 0.5`, keep the stronger chunk.

Ordering:

```text
rerank
-> dedupe by message_ids
-> context assembly
```

Reaction boost remains out of Phase 6 until `reaction_count` exists in the Qdrant payload.

Expected outcome:

Repeated evidence is reduced before the LLM sees the final context.

Note:

Full reply-root dedupe can be added later when `root_message_id` is available in the Qdrant payload.

## Phase 7: Context Assembly And Prompt Contract
Implement the context block exactly from the retrieval/context/prompt contract.

Implementation artifact:

`workflows/n8n/rag-active-call-phase-7-context-prompt-contract.json`

Include:

- channel
- thread
- date range
- authors
- reranker score
- message IDs
- Discord link
- chunk text

Expected outcome:

Gemini receives structured, citable context instead of raw unformatted chunks.

Implementation note:

Phase 7 separates context assembly from the dedupe decision. `Assemble Context Contract` is the source of truth for the final context block, prompt hash, selected context count, token estimates, and budget-gate refusal. Phoenix should show `context.assembled`, `context.overflow`, or `context.insufficient` spans for this step.

Budget gate:

- Assemble up to five chunks.
- Estimate context tokens.
- If the context exceeds the configured budget, drop the lowest-scored selected chunk and recompute.
- Continue until under budget.
- If fewer than three chunks remain, refuse with `context_token_budget_insufficient`.
- Log `context.overflow` for trimming and `context.insufficient` for refusal.

## Phase 8: Regression Evaluation Harness
Add an automated regression harness before expanding into passive listener behavior.

Reason:

The active-call path now has retrieval, reranking, dedupe, context assembly, prompt construction, Discord dispatch, Postgres state, and Phoenix traces. Manual validation is no longer enough to know whether a workflow change improved quality or simply passed one demo question.

The regression harness should run the curated question set from `scripts/regression_questions.jsonl` and any additional questions provided by the team. The file format and maintenance rules are documented in `docs/Regression README.md`.

It needs:

- one repeatable intake/routing entry point for running a batch of questions
- a shared RAG core workflow so regression, CI, active calls, and later passive calls do not fork retrieval logic
- support for retrieval-only evaluation so retrieval, rerank, dedupe, and context assembly can be tested without Gemini cost or variability
- support for full answer/refusal evaluation when Gemini behavior is being tested
- three supported run paths: maintainer manual run, no-secret CI run, and Shilpi/manual evaluator run without Gil's Discord webhook or Gemini API key
- one durable row per regression run and one durable row per question result
- expected outcome fields for grounded answer, correct refusal, partial context, no context, stale context, adversarial, safety, and PII cases
- actual outcome fields for status, retrieval status, refusal reason, selected chunks, scores, citations, answer length, latency, and trace link
- summary reporting for pass rate, false refusals, missed refusals, citation failures, no-context violations, and latency
- optional derived label writing to `rag_eval_labels` with `source = regression`, disabled by default until the team chooses to treat automated regression labels as dashboard inputs

Expected outcome:

The team can run the same question set after each retrieval, context, prompt, or schema change through the same shared RAG core used by production paths, then see whether quality improved, regressed, or needs review.

Workflow design:

```text
RAG Intake + Routing
-> Shared RAG Core
-> mode-specific output writers
```

The intake workflow identifies `trigger_source`, sets `run_mode`, sets `response_mode`, and enforces `allow_gemini` / `allow_discord_post`. The shared core owns normalization, embedding, Qdrant retrieval, reranking, dedupe, context assembly, refusal gates, optional Gemini execution, and core Phoenix checkpoints.

Exit criteria:

- the regression question file format is documented
- the harness can run at least retrieval-only mode
- retrieval-only mode can run without Gemini, Discord, or personal credentials
- regression does not duplicate the RAG retrieval/rerank/dedupe/context logic
- the harness can run the known Meta partnership seed case
- each run persists enough evidence to debug failures outside the n8n editor
- false refusal, missed refusal, no-context violation, and citation failure categories are explicit
- results are suitable for later weekly quality metrics and human review

Deferred CI work:

CI execution is intentionally deferred until the manual and batch regression paths are stable. A later phase should add a GitHub Actions workflow that validates the JSONL file and runs a no-secret retrieval-only regression path against either local services or a restored Qdrant snapshot. CI should start as non-blocking or structural-only until the team agrees on hard quality gates.

## Phase 9: Passive Listener
Add passive listener behavior only after the active-call path is stable.

Reason:

Passive listening has higher noise risk than active calls.

It needs:

- stricter relevance rules
- rate limiting
- ignored-event logging
- possibly higher retrieval thresholds
- silent drop behavior for weak context

Expected outcome:

Passive behavior expands coverage without making the bot noisy.

## Phase 10: Feedback Correlation
Add Discord reaction monitoring after bot responses store `discord_response_message_id`.

Flow:

```text
reaction event
-> check whether target message is a bot response
-> look up transaction by discord_response_message_id
-> normalize feedback
-> upsert feedback row
-> flag review candidates when feedback is negative or explicit critique
-> update trace or metrics
```

Expected outcome:

User reactions can be tied back to the original retrieval and answer transaction.

Schema contract:

- `feedback_source` stores where the signal came from: `reaction`, `context_menu`, `slash_command`, `form`, or `manual`.
- `feedback_type` remains the legacy normalized type: `positive`, `negative`, or `explicit`.
- `feedback_value` stores the normalized sentiment or structured value.
- Negative reactions and explicit critique set `review_candidate = true` and `review_status = pending`.
- Unmatched feedback writes `matched = false` and is excluded from weekly quality metrics until linked.

## Phase 11: Weekly Metrics And Alerts
Add reporting after transactions, retrieval, refusals, responses, and feedback are flowing.

Weekly metrics:

- read Postgres source tables
- compute weekly rollup
- upsert `rag_weekly_metrics`
- post `#bot-metrics` digest

Alerts:

- monitor refusal and quality thresholds
- monitor latency thresholds
- monitor dispatch failures
- post warning or critical notifications

Expected outcome:

The system becomes measurable and maintainable without manual query assembly.

## Phase 12: Gemini Prompt Hardening And Stress Testing
Harden the generation and refusal behavior after the retrieval pipeline, dedupe, context assembly, feedback, and observability paths are stable.

Scope:

- run the curated regression question set across grounded-answer, partial-context, no-context, subjective, stale-context, adversarial, and PII cases
- verify that supported questions produce grounded answers and unsupported questions produce the exact refusal
- test multi-part questions where only some parts are supported
- verify every key claim has a valid source citation
- enforce Discord's 2,000-character limit with a 1,900-character generation target and deterministic pre-dispatch validation
- record prompt version, model version, finish reason, token usage, answer length, refusal reason, and latency
- measure answer consistency across repeated runs of the same question
- tune the prompt and generation settings without weakening the retrieval and reranker gates
- define launch thresholds for groundedness, correct refusal, citation validity, response length, and latency

Expected outcome:

Gemini behavior is repeatable enough for launch, with regression evidence showing that prompt changes do not turn unsupported context into answers or valid context into unnecessary refusals.

Exit criteria:

- all required regression categories have reviewed expected outcomes
- grounded answers, correct refusals, and citation validity meet the evaluation thresholds
- no response sent to Discord exceeds 2,000 characters
- repeated runs expose and quantify model variability
- known prompt-quality and latency limitations are either resolved or explicitly accepted for launch

Required regression seed case:

```text
Question:
How does the partnership interview at Meta work, and do I need technical examples for it?

Category:
grounded multi-part answer

Expected behavior:
- answer the partnership-interview portion directly
- explicitly answer the technical-examples portion instead of leaving it implicit
- explain that retrieved community evidence frames the partnership interview primarily around cross-functional collaboration, communication, stakeholder alignment, and influence without authority
- state that the retrieved evidence does not show technical examples are required for the partnership interview specifically
- qualify that technical or semi-technical examples can still be useful when they demonstrate cross-functional influence, delivery, tradeoffs, metrics, or collaboration with technical and non-technical stakeholders
- distinguish partnership-interview evidence from separate Meta technical/program/system-design interview evidence when both appear in retrieval
- cite the June 18, 2024 `#tpm-interview-resources` source and at least one supporting `#interview-experience` source when used
- stay under the Discord response limit

Failure examples:
- answering only how the interview works while ignoring whether technical examples are needed
- treating technical-interview preparation evidence as proof that technical examples are required for the partnership interview
- refusing despite the retrieved context containing direct partnership-interview evidence
- answering without citations
```

## First Implementation Milestone
The first milestone should be:

```text
Active Discord mention
-> Qdrant retrieval
-> Gemini answer or refusal
-> Discord response
-> Postgres transaction
-> Phoenix trace
```

This milestone intentionally excludes passive listener behavior, feedback correlation, weekly metrics, and advanced alerting.

## Implementation Discipline
Each phase should end with:

- one working workflow path
- a known test question
- expected output
- Postgres evidence
- Phoenix trace evidence
- a short failure checklist

Do not add the next phase until the current phase can be validated from outside the n8n editor.
