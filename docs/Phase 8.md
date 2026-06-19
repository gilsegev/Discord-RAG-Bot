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
Shilpi / evaluator retrieval-only run
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
- prevent forbidden side effects, such as Discord posting from CI or Shilpi runs

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

The shared core must not know whether the request came from Discord, CI, Gil, or Shilpi except through explicit mode flags. That keeps the logic reusable and prevents workflow drift.

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
  "requested_by": "gil | ci | shilpi | bot"
}
```

Allowed behavior should be enforced from the flags, not from assumptions about who clicked the workflow.

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

### 3. Shilpi Manual Run Without Gil's Secrets

User:

AltCtrlDeliver / Shilpi or another trusted evaluator.

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
Shilpi opens limited tunnel
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

## Runner Script Role

A repo-owned script is still useful, but it should not own RAG logic.

Responsibilities:

- read `scripts/regression_questions.jsonl`
- validate required fields
- select all cases, one case, or a category
- call the RAG Intake + Routing workflow or its webhook entry point
- print and save the returned summary
- exit with a CI-friendly status code

The script is a client of the intake workflow, not a second implementation of retrieval.

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

- Add `workflows/n8n/rag-intake-routing-phase-8-active-call.json`.
- Preserve the known-working Phase 7 active-call RAG path.
- Add the Phase 8 intake mode contract fields.
- Validate that the active-call path still reaches Discord before adding regression and CI branches.
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

CI and Shilpi modes must set `allow_gemini = false` and `allow_discord_post = false`.

### Step 4: Add Runner Script

Create a small repo-owned command, for example:

```text
npm run regression:run -- --mode retrieval_only --cases all
```

The script should:

- read `.env.local`
- call the n8n webhook
- pass selected cases
- print summary
- save output artifact

### Step 5: Add CI Job

Add a GitHub Actions workflow that initially runs:

- JSONL schema validation
- regression runner dry-run or retrieval-only mode when services are available

CI should start as non-blocking or structural-only until the team agrees on hard quality gates.

### Step 6: Add Shilpi Instructions

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
- Shilpi can run retrieval-only without Gemini or Discord credentials.
- CI can validate the regression file and run the no-secret path or a dry-run equivalent.
- Every run has a durable run row and result rows.
- Phoenix traces show case-level execution.
- Failures are categorized clearly enough to debug the pipeline stage that regressed.

## Open Decisions

- Whether CI should restore a Qdrant snapshot artifact or rebuild Qdrant from checked-in logs.
- Which gates should fail CI immediately versus produce a warning during calibration.
- Whether full-answer mode should ever post to Discord, or only store generated text in Postgres.
- Whether Shilpi should run through n8n UI only or also have runner-script access through a constrained command.
- Whether regression result rows should later be promoted into `rag_eval_labels` manually, via LLM judge, or both.
