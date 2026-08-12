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
- Refuses before Gemini when no candidates have `reranker_score > 0`. The Stage 1 minimum of three is not reused after reranking.
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

Dedupe refuses only when no unique candidates remain. The Stage 1 minimum of three is not reused after dedupe.

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
- three supported run paths: maintainer manual run, no-secret CI run, and AltCtrlDeliver/manual evaluator run without Gil's Discord webhook or Gemini API key
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

## Phase 9C: Continuous Incremental Ingestion

Phase 9C implements the production path approved and merged in
[PR #24](https://github.com/gilsegev/Discord-RAG-Bot/pull/24). The
[continuous incremental ingestion design](continuous-incremental-ingestion-design.md)
is authoritative; export fingerprinting remains a
baseline/backfill/recovery mechanism rather than the normal update path.

The goal is to capture new eligible Discord messages as they arrive, then update
only the affected reply conversations or recent time windows. It does not embed
each message immediately and it does not change the chunking rules in
`ingestion/chunker.py`.

### Phase 9C.1: Capture before routing

**Status:** Implemented in the Phase 9 intake workflow

The listener adds creation time, reply parent, parent channel, thread,
attachment, message type, and author-display metadata to each accepted
`MESSAGE_CREATE` envelope.

n8n remains the persistence owner:

```text
Discord Gateway listener
-> Normalize Intake
-> validate corpus capture policy
-> Capture Discord Message in Postgres
-> restore the intake envelope
-> existing active/passive/ignored routing
```

The Postgres operation is the first durable intake action. It atomically inserts
one `rag_discord_messages` row and one `rag_pending_chunk_work` row. Discord
message ID conflict handling makes delivery idempotent, and database-backed
duplicate state prevents a repeated direct mention from executing RAG twice.

Capture eligibility mirrors the historical export parser:

- only the configured guild and corpus-eligible parent channels
- human-authored default/reply messages
- no bots, webhooks, or system events
- nonempty supported content, with attachment-only messages retained
- export-compatible mention, channel, role, emoji, URL, and whitespace
  normalization

Manual, regression, CI, and evaluator workflow invocations are not capture
candidates. Existing active/passive routing continues after capture.

The MVP accepts the existing delivery limitation: if the listener queue
overflows, n8n is unreachable, or n8n fails before the insert, that message can
be missed. The listener logs the failure; durable retry/history recovery is
deferred.

Exit criteria:

- the additive migration applies to the deployed `ragbot` database
- an eligible message creates one message and one pending-work row
- thread replies preserve parent-channel, thread, and direct-parent IDs
- a repeated message ID leaves one row in each table and routes as
  `duplicate_event`
- bot, webhook, system, wrong-guild, excluded, and corpus-ineligible messages do
  not enter the capture tables
- regression/manual invocations do not enter the capture tables
- active and passive behavior has no retrieval-quality regression

### Phase 9C.2: Seed chunk ownership manifest

**Status:** Completed in PR #45

Phase 9C.2 creates the ownership baseline needed for safe targeted replacement.
It is additive: it must not consume pending work, re-embed chunks, or mutate the
production Qdrant collection.

Implementation:

1. Apply the additive manifest and corpus-version migration to `ragbot`.
2. Read every current Qdrant point and derive one deterministic logical owner
   from its existing payload metadata.
3. Dry-run first and fail on missing, conflicting, or ambiguous ownership.
4. Seed one active manifest row per Qdrant point, preserving point ID,
   per-piece message IDs, first/last message IDs, and version metadata.
5. Add indexed lookup paths for message, logical group, and reply root.
6. Verify Postgres against a fresh Qdrant scan.
7. Rerun the seed and prove it is idempotent.

The implementation entry point is `python -m ingestion.chunk_manifest`. It is
read-only by default; use `--output <plan.json>` for the reviewed plan,
`--verify-plan <plan.json>` for a fresh Qdrant comparison, and add `--apply`
plus `--database-url` only after verification to seed Postgres atomically.

Required evidence:

- Qdrant point count equals active manifest row count
- no duplicate active `point_id`
- no Qdrant point is missing from the manifest
- no active manifest point is missing from Qdrant
- message IDs and ownership metadata match Qdrant payloads
- the seed run reports all ambiguous/unowned points and exits nonzero
- a second run changes zero manifest rows
- Qdrant collection count and point digest are unchanged before versus after
- the complete Phase 8 retrieval regression remains at the accepted baseline

Operational checks run from a Railway shell connected to the `ragbot` database:

```sql
SELECT COUNT(*) AS active_manifest_points
FROM rag_chunk_manifest
WHERE active;

SELECT point_id, COUNT(*)
FROM rag_chunk_manifest
WHERE active
GROUP BY point_id
HAVING COUNT(*) <> 1;

SELECT cv.corpus_version_id, m.chunker_version, m.embedding_version, COUNT(*)
FROM rag_chunk_manifest AS m
JOIN rag_corpus_versions AS cv
  ON cv.ingestion_run_id = m.ingestion_run_id
WHERE m.active AND cv.status = 'healthy'
GROUP BY cv.corpus_version_id, m.chunker_version, m.embedding_version;
```

The implementation validator remains the source of truth for cross-system
comparisons because SQL alone cannot prove that each Qdrant point and payload
matches its manifest row. A failed validation leaves Phase 9C.2 unaccepted and
does not authorize production replacement.

### Phase 9C.3: Offline planner and shadow rechunking

**Status:** Implemented in the Phase 9C.3 PR

Phase 9C.3 turns pending capture rows into deterministic replacement plans
without changing the production corpus.

Plan lifecycle is deliberately small:

```text
planned -> shadow_validated
   |
   +-> deferred / failed
```

`shadow_validated` is the only plan state eligible for Phase 9C.4. It requires:

- production-model embedding of every replacement chunk
- embedded count equal to replacement count, with every vector 768 dimensions
- observed embedding model/version matching the declared version
- current source corpus version/digest binding
- zero Qdrant mutations

A structurally complete plan without full embedding evidence remains `planned`.
Deferred-only work remains `deferred`. Deterministic replanning, exact ownership
and text digests, affected-scope selection, and fixture equivalence remain
automated planner validations rather than persisted production attestations.
Phase 9C.4 rechecks the source corpus version/digest immediately before
maintenance. A stale plan is rejected and the rejection is recorded on the run;
the plan does not need another lifecycle status.

Implementation:

1. Read a fixed pending-work cutoff without claiming or completing work.
2. Coalesce replies by proven conversation root and reproduce the v10
   non-reply window behavior. A singleton remains buffered until the next
   same-scope message; once a window contains two messages, a later message
   beyond 15 minutes starts the next window.
3. Resolve affected existing points through `rag_chunk_manifest` and Qdrant
   payload timestamps, including the configured two-message overlap.
4. Run the existing v10 chunker on the bounded region.
5. Produce stable old-point and replacement-point IDs. A final unmatched
   singleton remains `deferred`, matching the full v10 chunker.
6. Optionally call the production embedding service for every replacement chunk
   to validate the 768-dimension contract and measure Railway throughput.
7. Persist the immutable plan and evidence in
   `rag_chunk_replacement_plans` without changing Qdrant or pending-work status.

The entry point is `python -m ingestion.incremental_planner`. It requires
`--database-url` and `--qdrant-url`; `--embedder-url` adds live shadow embedding,
`--output` writes the review artifact, and `--persist` stores the validated plan.

Exit criteria:

- reply and window work is deterministically coalesced
- repeated planning at the same cutoff produces the same plan ID and digest
- selected old points belong only to affected channel/thread/conversation scopes
- fixture shadow output matches full v10 chunker output for the affected scope
- every shadow embedding is exactly 768 dimensions and reports the expected
  production model/version
- persisted plans bind to the current source corpus version/digest
- output and static validation report zero Qdrant mutations
- measured replacement throughput and duration are recorded for Phase 9C.4
- the complete Phase 8 regression remains at the accepted baseline

Initial Railway validation measured 10 representative 768-dimension
embeddings in 5.361 seconds (111.92 chunks/minute). Historical transactions,
excluding `n8n-regression` and `rag-bot-testing`, recorded 81 real-server
messages across the seven complete days from July 21 through July 27: 11.6
messages/day on average and a peak of 23. At the measured embedding rate, the
worst-case one-chunk-per-message embedding time is approximately 6 seconds for
an average day, 12 seconds for the observed peak, and 27 seconds for a
conservative 50-message day.

The complete 48-case regression consistently takes 118–120 seconds. Including
the observed request-drain tail (p95 approximately 16 seconds, maximum 27
seconds) and a buffer for Qdrant replacement plus structural verification, the
expected Phase 9C.4 maintenance window is approximately 2.5–3 minutes for a
typical day and 3–4 minutes for a conservative peak. The planner itself runs
before maintenance and does not contribute to user-visible downtime.

### Phase 9C.3.5: Incremental-run state and observability

**Status:** Implemented as the prerequisite for Phase 9C.4

This interim phase keeps production mutation focused. It adds these durable
tables to the `ragbot` Postgres database:

- `rag_incremental_runs`: one permanent summary per run
- `rag_runtime_state`: singleton serving/maintenance coordination
- `rag_active_execution_leases`: bounded leases for draining RAG work

It also tightens the Phase 9C.3 persistence path so `shadow_validated` cannot be
written unless complete replacement embedding count, dimension, observed
model/version, source binding/freshness, and zero-mutation evidence satisfies
the contract above.

Postgres is the durable source of truth for current state and outcome. The run
row records phase results and timestamps, reconciled counts, failures,
regression, retries, and rollback. Phoenix receives correlated spans using the
same `incremental_run_id` and is the detailed transition timeline.

The coordinator exposes only narrow transactional maintenance-enter and
maintenance-exit operations. Enter verifies the eligible plan and source,
closes the serving gate, and associates the runtime state with the run. Exit
records the durable outcome and reopens serving. This is not a generalized pair
of run/runtime state machines.

Run evidence includes plan/corpus IDs, cutoff, timestamps, state,
pending/claimed/processed/deferred message counts, affected groups,
old/replacement/new/reused/deleted point counts, snapshot bytes/digest, phase
durations, regression run/result, retries, failure step/reason, and rollback.

**Gate:** valid and invalid enter/exit requests are tested; duplicate workflow
delivery is idempotent; counts reconcile to source rows; and Phoenix spans
correlate to the durable Postgres run.

Implementation artifacts:

- `10-phase9c35-run-state-observability-migration.sql` adds revisioned runtime
  state, three narrow maintenance operations, durable run summaries, and
  bounded execution leases.
- `incremental_planner.py` requires source-corpus freshness, complete production
  embedding count, exactly 768 dimensions, observed model/version agreement,
  and zero Qdrant mutations before persisting `shadow_validated`.
- `RAG Incremental Coordinator - Phase 9C.3.5` is manual and disabled. Its
  simulation path persists the run summary while keeping runtime `serving`,
  pending work unclaimed, and Qdrant untouched.
- The shared RAG core acquires, heartbeats, and releases a bounded execution
  lease. The future Phase 9C.4 drain transition uses these leases instead of a
  second RAG execution path.

### Phase 9C.4: Maintenance mode and production replacement

**Status:** Completed manually in production on August 12, 2026; remains
feature-flagged

Phase 9C.4 accepts only `shadow_validated` plans and revalidates their source
corpus version/digest at application time. A stale plan is rejected before
maintenance and the run records the rejection reason.

The transactional enter operation gates both shared intake and RAG core before
draining begins. Draining means waiting only for in-flight online RAG work that
already passed the serving gate and may be using Qdrant, the reranker, or
Gemini. Durable Discord capture continues and every new RAG read is gated.
After the leases drain, the coordinator snapshots affected old points, executes
deterministic replacement, verifies the corpus, and runs regression while
gated. The transactional exit operation records the outcome and restores
serving only after success or completed recovery.

Rollback snapshots are created for every production run and retained for
14 days. The first production canary also requires a full Qdrant snapshot.
Snapshots cannot expire while a related run is `review_needed` or before the
replacement passes structural verification and regression.

Required validation:

- active calls receive the maintenance response without reaching Qdrant,
  reranker, or Gemini
- passive calls are captured as post-cutoff pending work without entering RAG
- maintenance enter/exit behaves correctly under races, retries, and failures
- full regression before replacement and before reopening matches baseline
- injected failure after every mutation step is retryable or reversible
- no normal query observes a partially updated corpus

The coordinator is an n8n workflow. Python provides deterministic
planning/chunking and narrow Qdrant operations invoked by n8n; no separate
always-on orchestration service is introduced.

The complete Phase 8 regression suite is part of the Phase 9C.4 safety gate,
not deferred to scheduled operation. Run it before replacement to confirm the
starting baseline and after structural verification. Serving reopens only when
the post-replacement result matches the accepted baseline or recovery has
completed.

The July 2026 138.4-minute full rebuild ran on Gil's higher-performance 8-core
workstation. Incremental production execution now runs on Railway, so later
batch and maintenance budgets must use measured Railway throughput rather than
the retired Oracle host or workstation result.

Production acceptance evidence:

- run `phase9c35-live-simulation-20260729T045609Z` resumed the approved
  `shadow-1e95ba4d2fc1a7e74338` plan and completed successfully
- the accepted 48-case result was `43 pass / 1 fail / 4 review` both before
  replacement (`ddc41f28-4ced-4109-9943-1ab3c4e9a038`) and while gated after
  replacement (`76a598a2-f776-4b3d-b899-e77747e7eeb4`)
- Qdrant and the active manifest moved together from 32,756 to 32,759 points;
  six messages completed and three intentionally remained deferred/pending
- the run retained per-point rollback state for 14 days and created full
  Qdrant snapshot
  `tpm_unite_history-599516084158867-2026-08-12-17-30-38.snapshot`
- an ordinary active request was refused before retrieval during maintenance;
  a duplicate passive capture traversed the durable capture path and stopped
  before RAG; a post-reopen retrieval canary passed
- runtime returned to `serving` at revision 3 with corpus version
  `incremental-89dc2637cb6b4b3259a7` healthy

The production canary also caught and fixed two fail-closed wiring defects
before reopening: SQL `NULL` handling in maintenance admission and propagation
of `maintenance_validation_run_id` from intake into the shared core. Focused
tests now cover the latter, and the full regression proves the corrected path.

### Phase 9C.5: Scheduled-operation readiness

**Status:** Implemented and deployed inactive on August 12, 2026; schedule must
remain disabled until Phase 9C.6 completes

After Phase 9C.4 is proven manually, add the configurable low-traffic schedule,
run reporting, alerting, and operator runbook around the same coordinator. Keep
the schedule disabled until the one-time Phase 9C.6 catch-up succeeds. Do not
create a second replacement path for scheduled work.

Execution plan:

1. Add a small scheduled-controller workflow that calls the proven Phase 9C.4
   coordinator; it must not contain its own replacement logic.
2. Add configuration for enabled/disabled state, low-traffic cron time, batch
   limits, and maintenance/time budgets. Defaults are disabled and fail closed.
3. Refuse overlapping runs, stale/unvalidated plans, unhealthy capture, or a
   runtime that is not `serving` before the scheduled controller can drain it.
4. Store one durable run report with plan, cutoff, counts, durations, snapshot,
   regression, rollback, and final runtime/corpus state.
5. Send deduplicated operator alerts for preflight rejection, failed recovery,
   `review_needed`, or a runtime left outside `serving`; send a short success
   summary for completed runs.
6. Add the operator runbook for enabling/disabling the schedule, inspecting a
   run, retrying safely, rolling back, and escalating a stuck maintenance state.
7. Test the controller locally and deploy it inactive. Prove its manual dry-run
   and alert/report paths without changing Qdrant. Phase 9C.6 remains the next
   production mutation, and only its success may enable the schedule.

Exit criteria:

- the existing Phase 9C.4 coordinator remains the only apply/rollback path
- schedule configuration is visible, validated, and disabled in production
- dry-run, overlap, stale-plan, unhealthy-capture, failure, and success paths
  have automated tests and durable evidence
- an operator can understand and recover any run from the report and runbook
- normal serving and Discord capture remain unchanged

Production readiness evidence:

- the controller, scheduled runner, and alert-outbox workflows are deployed
  inactive in n8n
- Postgres has independent `schedule_enabled=false` and
  `catchup_completed=false` locks, with a 03:00 UTC default low-traffic cron
- production dry-run `phase9c5-prod-dryrun-final-20260812` stopped with
  `phase9c6_catchup_required`, reported 176 bounded pending messages, recorded
  one durable attempt and one deduplicated warning, and reported zero Qdrant
  mutations
- runtime remained `serving` at revision 3 and Qdrant remained at 32,759
  points before and after the proof
- local Postgres tests prove disabled/catch-up/overlap/plan-budget guards,
  idempotent alerting, durable reports, and drain-timeout recovery; workflow
  tests prove dry-run cannot dispatch, mutations remain delegated to the Phase
  9C.4 coordinator, regression runs before and after replacement, and failure
  branches use rollback
- alert delivery remains queued until a private operations destination is
  configured with `INCREMENTAL_ALERT_WEBHOOK_URL`; no destination or secret is
  stored in workflow JSON

### Phase 9C.6: One-time captured-message catch-up

**Status:** Execution in progress on the Phase 9C.6 PR

The operator-ready sequence, evidence requirements, stop rules, and manual
dependency are maintained in
[the Phase 9C.6 execution plan](Phase%209C.6%20execution%20plan.md).

Run one migration-style incremental batch covering all eligible Discord
messages after the last message represented by the current healthy Qdrant
corpus and at or before a fixed catch-up cutoff. New messages captured after
the cutoff remain pending for the normal scheduled path.

The owner accepted the unrecorded July 23-26 interval as a permanent historical
exclusion on August 12, 2026. The catch-up therefore starts at the first durable
capture row and must not claim that the excluded interval was recovered. It
still fails closed unless every captured row through the fixed cutoff has a
consistent work row and no work is orphaned or already claimed.

The catch-up reuses the Phase 9C.4 shadow-validated plan, maintenance gate,
lease drain, snapshots, deterministic replacement, manifest update, rollback,
structural verification, and pre/post full regression. Take a full Qdrant
snapshot before this first production run. Preserve the cutoff and before/after
message, work-row, point, manifest, corpus-version, and regression counts as
permanent migration evidence.

Production preflight evidence recorded on August 12, 2026:

- the healthy manifest's last represented Discord message is
  `1529684708294787083`, at approximately `2026-07-23 03:00:54 UTC`
- durable capture begins at `2026-07-26 18:35:40 UTC`
- the resulting unproven interval is approximately 3 days and 15 hours
- 182 captured messages are pending from July 26 through August 12, and none
  are represented by the current active manifest
- transaction history found 33 non-test records in the unproven interval:
  one is the already-manifested boundary message, 20 are real Discord IDs not
  in the manifest, and 12 use synthetic `replay-*` IDs

The 20 real IDs in
[the Phase 9C.6 known-gap checklist](phase9c6-known-gap-message-ids.csv) are
marked `accepted_exclusion` and are not imported. Synthetic replay IDs are also
excluded. This is an explicit availability tradeoff, not proof that the
historical interval is complete.

Exit criteria:

- the accepted July 23-26 exclusion is recorded durably and never represented
  as recovered coverage
- every captured row through the fixed cutoff has a consistent work row
- every eligible pre-cutoff work row is completed or explicitly deferred with
  a durable reason
- no pre-cutoff work remains silently pending or claimed
- Qdrant and the active manifest agree and the new corpus version is healthy
- the full regression matches the accepted baseline before serving reopens
- the Phase 9C.5 schedule remains disabled until this catch-up succeeds

## Phase 10: Feedback Correlation
Add shared Discord reaction monitoring after bot responses store
`discord_response_message_id`. Phase 10 is not gated on Phase 9B: active calls,
Phase 9/9B passive responses, and future response modes use the same path.

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

Reaction semantics:

- Different members' reactions are stored and counted independently.
- One member may retain both 👍 and 👎 on the same response, as Discord permits.
- Each normalized reaction has its own row; repeated adds are idempotent and a
  removal deletes only the row for that reaction.
- Weekly metrics derive positive-only, negative-only, and mixed member states
  from current rows. Mixed feedback remains reviewable because a negative
  reaction is present and must not be silently converted to positive.

## Phase 10B: Explicit Critique Categories And UX

Add structured critique capture after reaction correlation is stable. This
tracks the deferred context-menu, slash-command, or form UX, reason categories,
and optional free text described by the evaluation workstream. Do not finalize
categories or interaction design until the referenced Feedback & Reaction
Correlation Design is available and ratified.

Expected outcome:

Members can explain why an answer was unhelpful, and explicit critiques enter
the same human-review path without becoming automatic evaluation failures.

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

## Phase 13: Discord Direct Message Support
Add support for users to interact with the bot through Discord direct messages
(private messaging).

This phase is a placeholder for future design. Before implementation, define the
DM intake and response flow, user authorization and abuse controls, privacy and
data-retention rules, retrieval scope, observability requirements, and how DM
behavior reuses the shared RAG core without duplicating the existing guild
message path.

Expected outcome:

The bot can receive and respond to supported questions in Discord DMs while
preserving the project's retrieval, safety, privacy, and observability
requirements.
