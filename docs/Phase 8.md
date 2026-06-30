# Phase 8: Regression Evaluation Harness

## Status

Planning branch: `phase-8-regression-evaluation`

## Goal

Build a repeatable way to run curated regression questions through the active-call RAG pipeline before adding passive listener behavior.

The harness should make quality measurable across a question set instead of relying on one-off manual n8n runs.

## Why This Comes Before Passive Listener

Passive listening increases risk because the bot decides when to engage without a direct user request. Before enabling that behavior, the active-call path needs repeatable regression evidence for:

- grounded answers
- correct refusals
- false refusals
- missed refusals
- partial-context questions
- multi-part questions
- citation validity
- answer length
- latency
- no-context violations
- PII and safety behavior

## Inputs

Canonical seed files:

- [Regression README.md](Regression%20README.md)
- [regression_questions.jsonl](../scripts/regression_questions.jsonl)

The v1 set has 45 cases:

| Category | Count | Expected Action |
|---|---:|---|
| `happy_path` | 16 | answer |
| `nuanced_subjective` | 8 | answer with caveat |
| `personal_context` | 5 | answer as community wisdom with caveat |
| `no_context_refusal` | 10 | refuse |
| `adversarial_pii` | 6 | refuse or block PII |

## Design Principles

- The regression harness should test the same retrieval, rerank, dedupe, context assembly, and prompt contracts used by the production active-call flow.
- Regression must not fork or copy the RAG execution logic.
- Active calls, passive candidates, manual regression, CI regression, and evaluator runs should enter through one intake/routing contract.
- The shared RAG core should be the only place that owns retrieval, rerank, dedupe, context assembly, prompt construction, refusal decisions, and optional Gemini execution.
- Retrieval-only mode must not require Gemini, Discord, or Gil's personal credentials.
- Full-answer mode may use Gemini, but only through bot-owned credentials.
- CI should default to a no-secret retrieval-only run.
- Each run must produce durable evidence outside the n8n editor.
- Regression labels are evaluation data, not live user feedback.
- Results should be inspectable from Postgres and traceable in Phoenix.

## Target Workflow Architecture

Use one intake/routing workflow and one shared RAG core workflow.

```text
Discord active call
Discord passive candidate
Gil manual regression run
CI regression run
AltCtrlDeliver / evaluator retrieval-only run
        |
        v
RAG Intake + Routing workflow
        |
        v
Shared RAG Core workflow
        |
        v
Mode-specific outputs:
- regression result rows
- CI summary artifact
- Discord response
- Phoenix trace
- Postgres transaction/eval evidence
```

### RAG Intake + Routing Workflow

This workflow owns who or what is calling the system.

Responsibilities:

- identify `trigger_source`
- validate the request shape
- create or attach `transaction_id`
- create `regression_run_id` when the request is part of a regression batch
- set mode flags
- call the shared RAG core workflow
- route the returned result to the right output writers
- prevent forbidden side effects, such as Discord posting from CI or AltCtrlDeliver runs

### Shared RAG Core Workflow

This workflow owns the RAG behavior.

Responsibilities:

- normalize query
- embed query
- search Qdrant
- rerank candidates
- apply dedupe
- assemble context
- apply retrieval/context refusal gates
- call Gemini only when allowed
- return a structured result object
- emit Phoenix checkpoints for the RAG execution path

The shared core must not know whether the request came from Discord, CI, Gil, or AltCtrlDeliver except through explicit mode flags. That keeps the logic reusable and prevents workflow drift.

### Mode Contract

Every request should carry an explicit mode object.

```json
{
  "trigger_source": "discord_active | discord_passive | regression_manual | regression_ci | evaluator_manual",
  "run_mode": "retrieval_only | full_answer",
  "response_mode": "postgres_only | discord_test | discord_live | ci_artifact",
  "allow_gemini": false,
  "allow_discord_post": false,
  "case_id": "RQ-001",
  "regression_run_id": "optional",
  "requested_by": "gil | ci | altctrldeliver | bot"
}
```

Allowed behavior should be enforced from the flags, not from assumptions about who clicked the workflow.

### Channel Scope Contract

Regression cases may include `channel_scope`.

- `all` means search the full indexed corpus.
- `in #channel-name` means constrain retrieval to that channel for that regression case.

This is a regression/request-level test control, not the default production behavior. Normal active-call bot answers should continue to search across the indexed corpus unless the user or calling workflow explicitly asks for a scoped answer.

## Required Run Modes

### 1. Maintainer Manual Run

User:

Gil or another maintainer with production-like credentials.

Purpose:

Run the full path when validating answer quality, prompt behavior, Discord-safe response length, refusal wording, and end-to-end latency.

Flow:

```text
manual trigger
-> RAG Intake + Routing
-> Shared RAG Core
-> optional Gemini when allow_gemini = true
-> optional test Discord post when allow_discord_post = true
-> regression result rows
-> Phoenix trace spans
```

Credentials:

- May use Gemini.
- May use a test Discord webhook.
- Should use bot-owned credentials where possible, not personal credentials.

### 2. CI Run After PRs And Changes

User:

GitHub Actions or equivalent CI.

Purpose:

Catch retrieval, rerank, dedupe, context assembly, schema, and prompt-contract regressions after every PR or material change.

Default mode:

`retrieval_only`

Flow:

```text
checkout repo
-> validate regression JSONL schema
-> start required local services or restore a Qdrant snapshot when available
-> call RAG Intake + Routing with trigger_source = regression_ci
-> Shared RAG Core runs retrieval_only
-> write machine-readable summary artifact
-> fail CI only on structural errors or configured hard gates
```

Credential policy:

- No Gemini key.
- No Discord webhook.
- No production server access.
- No personal secrets.

Expected CI output:

- JSON summary artifact
- optional CSV/Markdown summary
- pass/fail counts by category
- false refusal count
- missed refusal count
- no-context violation count
- context assembly failures
- latency summary

Full-answer CI:

This can be added later using bot-owned repository secrets after the retrieval-only harness is stable. It should not use Gil's personal Gemini key.

### 3. AltCtrlDeliver Manual Run Without Gil's Secrets

User:

AltCtrlDeliver or another trusted evaluator.

Purpose:

Run and train against regression questions without receiving Gil's Discord webhook or Gemini API key.

Default mode:

`retrieval_only`

Access model:

- Use limited SSH tunnel access to the Oracle server.
- Expose only the n8n UI and read/debug surfaces needed for regression.
- Use the repo-owned n8n instance.
- Use a regression workflow that does not contain Discord or Gemini nodes.
- Store results in Postgres and Phoenix for review.

Flow:

```text
AltCtrlDeliver opens limited tunnel
-> opens RAG Intake + Routing workflow
-> selects retrieval_only mode and question subset
-> runs batch through Shared RAG Core
-> reviews Postgres/Phoenix results
-> exports summary for threshold and label review
```

Credential policy:

- No Discord webhook.
- No Gemini key.
- No broad shell access required for ordinary runs.

## Regression Batch Runner Role
Regression execution is owned by an n8n workflow, not by a separate repo script.

The batch runner workflow:

- embeds the versioned cases from [regression_questions.jsonl](../scripts/regression_questions.jsonl)
- accepts filters such as `cases`, `category`, and `limit`
- iterates over the selected cases
- calls `RAG Intake + Routing - Phase 8` once per case through the internal n8n intake webhook
- defaults to `run_mode = retrieval_only`
- defaults to `allow_gemini = false`
- defaults to `allow_discord_post = false`
- writes one `rag_regression_runs` row per batch
- writes one `rag_regression_results` row per case
- returns a compact JSON summary to the caller

The runner does not own retrieval, rerank, dedupe, context assembly, or answer generation. It is an orchestrator around the shared intake and RAG core workflows.

A repo-owned CI helper script may be added later, but only as a client that triggers this n8n batch workflow and saves the returned artifact. It must not become a second RAG implementation.

## Persistence Model

Add two tables.

### `rag_regression_runs`

One row per batch run.

Suggested fields:

- `run_id`
- `run_mode`
- `trigger_source`
- `question_file`
- `question_file_hash`
- `workflow_name`
- `workflow_version`
- `git_sha`
- `started_at`
- `completed_at`
- `status`
- `case_count`
- `pass_count`
- `fail_count`
- `review_count`
- `summary_json`

### `rag_regression_results`

One row per question result.

Suggested fields:

- `result_id`
- `run_id`
- `case_id`
- `category`
- `question`
- `expected_action`
- `expected_caveat`
- `expected_flags`
- `expected_behavior`
- `transaction_id`
- `trace_id`
- `actual_status`
- `retrieval_status`
- `refusal_reason`
- `selected_context_count`
- `selected_channels`
- `selected_chunk_ids`
- `retrieval_scores`
- `reranker_scores`
- `context_token_estimate`
- `answer_length`
- `citation_status`
- `latency_ms`
- `outcome`
- `failure_type`
- `review_notes`
- `created_at`

Relationship to `rag_eval_labels`:

Regression results are run evidence. Human or judge scoring can later create `rag_eval_labels` rows from those results. Do not write automatic labels until the grading policy is explicit.

## Outcome Vocabulary

Use explicit result categories so regressions are easy to debug:

- `pass`
- `review_needed`
- `false_refusal`
- `missed_refusal`
- `no_context_violation`
- `citation_failure`
- `context_assembly_failure`
- `pii_safety_failure`
- `workflow_failure`

## Phase 8 Implementation Plan

### Step 1: Commit Regression Inputs

- Add [Regression README.md](Regression%20README.md).
- Add [regression_questions.jsonl](../scripts/regression_questions.jsonl).
- Validate JSONL format in a small script or test.

### Step 2: Add Regression Schema

- Add fresh-install SQL for `rag_regression_runs` and `rag_regression_results`.
- Add an additive migration for the running Oracle Postgres database.
- Index by `run_id`, `case_id`, `category`, `outcome`, and `created_at`.

### Step 3: Build Shared Intake/Core Workflows

Create or refactor toward:

- `workflows/n8n/rag-intake-routing.json`
- `workflows/n8n/rag-core-execution.json`

The first Phase 8 implementation can limit the intake workflow to regression modes, but it should use the same contract we expect active and passive calls to use later.

Implementation slice 1:

- Add `workflows/n8n/rag-intake-routing-phase-8.json`.
- Add `workflows/n8n/rag-core-execution-phase-8.json`.
- Preserve the known-working Phase 7 RAG internals inside the shared core.
- Keep transaction setup, mode flags, Discord posting, and final transaction updates in intake.
- Validate that the active-call path still reaches Discord through `Intake -> Execute RAG Core -> Discord` before adding regression and CI branches.
- Do not build a separate regression-only RAG copy.

Intake nodes:

- `Manual Trigger`
- `Set Request Mode`
- `Load Regression Cases`
- `Split In Batches`
- `Create Regression Run`
- `Prepare Case`
- `Execute RAG Core`
- `Classify Retrieval Outcome`
- `Write Regression Result`
- `Emit Phoenix Case Completed`
- `Finalize Regression Run`

Core nodes:

- `Normalize Query`
- `Emit Phoenix Core Started`
- `Query Embedding`
- `Qdrant Vector Search`
- `Rerank Candidates`
- `Dedupe And Context Decision`
- `Assemble Context Contract`
- `Gemini Generation` only when `allow_gemini = true`
- `Return RAG Result`

CI and AltCtrlDeliver modes must set `allow_gemini = false` and `allow_discord_post = false`.

### Step 4: Add n8n Regression Batch Runner
Add `workflows/n8n/rag-regression-batch-runner-phase-8.json`.

The workflow should:

- expose a batch webhook for manual, evaluator, and later CI calls
- load the versioned regression cases
- filter by one case, a comma-separated case list, category, or limit
- call `RAG Intake + Routing - Phase 8` once per selected case via `rag-intake-phase-8`
- pass `channel_scope` through to the shared RAG core so channel-scoped regression cases can apply a Qdrant payload filter
- force Discord posting off by default
- keep Gemini off by default
- persist run and case evidence to Postgres
- return a summary payload with counts and per-case outcomes

Default request:

```json
{
  "cases": "all",
  "mode": "retrieval_only",
  "allow_gemini": false,
  "allow_discord_post": false
}
```

The workflow may support full-answer mode for maintainer calibration, but retrieval-only remains the safe default for AltCtrlDeliver and CI.

### Step 5: Add CI Job

Add a GitHub Actions workflow that initially runs:

- JSONL schema validation
- regression runner dry-run or retrieval-only mode when services are available

CI should start as non-blocking or structural-only until the team agrees on hard quality gates.

### Step 6: Add AltCtrlDeliver Instructions

Update the developer evaluation access doc with:

- tunnel command
- n8n URL
- regression workflow name
- run mode
- expected output tables
- how to export results
- what access is intentionally not provided

### Step 7: Add Full-Answer Mode

After retrieval-only is stable:

- use the same RAG Intake + Routing and Shared RAG Core contract
- call Gemini using bot-owned credentials
- keep Discord posting disabled by default
- validate refusal wording, citation presence, answer length, and caveat requirements

## Success Criteria

- Regression inputs are versioned in PR20.
- A maintainer can run at least one known seed case manually.
- AltCtrlDeliver can run retrieval-only without Gemini or Discord credentials.
- CI can validate the regression file and run the no-secret path or a dry-run equivalent.
- Every run has a durable run row and result rows.
- Phoenix traces show case-level execution.
- Failures are categorized clearly enough to debug the pipeline stage that regressed.

## Full Regression Diagnosis

The first full retrieval-only regression run showed that Phase 8 is producing useful quality signal rather than exposing workflow bugs.

Observed false refusals fell into three expected buckets:

- corpus gaps where the question asks for a specific angle not strongly represented in the indexed logs
- intentional minimum-evidence refusals where only one strong source exists and the three-candidate gate blocks thin answers
- eval-case calibration candidates where the expected behavior may need rewriting or relabeling

Do not tune reranker thresholds from this first run alone. Threshold changes should happen in a dedicated calibration PR after reviewing the refusal/safety cases together.

## Open Decisions

- Whether CI should restore a Qdrant snapshot artifact or rebuild Qdrant from checked-in logs.
- Which gates should fail CI immediately versus produce a warning during calibration.
- Whether full-answer mode should ever post to Discord, or only store generated text in Postgres.
- Whether AltCtrlDeliver should run through n8n UI only or also have runner-script access through a constrained command.
- Whether regression result rows should later be promoted into `rag_eval_labels` manually, via LLM judge, or both.
