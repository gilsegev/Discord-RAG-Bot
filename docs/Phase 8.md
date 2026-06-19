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
- Retrieval-only mode must not require Gemini, Discord, or Gil's personal credentials.
- Full-answer mode may use Gemini, but only through bot-owned credentials.
- CI should default to a no-secret retrieval-only run.
- Each run must produce durable evidence outside the n8n editor.
- Regression labels are evaluation data, not live user feedback.
- Results should be inspectable from Postgres and traceable in Phoenix.

## Required Run Modes

### 1. Maintainer Manual Run

User:

Gil or another maintainer with production-like credentials.

Purpose:

Run the full path when validating answer quality, prompt behavior, Discord-safe response length, refusal wording, and end-to-end latency.

Flow:

```text
manual trigger or n8n webhook
-> load selected regression cases
-> run each question through active-call retrieval/rerank/dedupe/context assembly
-> call Gemini when mode = full_answer
-> optionally post to a test Discord webhook
-> persist run and per-question results
-> emit Phoenix trace spans
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
-> start required local services or restore a Qdrant snapshot
-> run retrieval-only regression harness
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
-> opens n8n regression workflow
-> selects retrieval_only mode and question subset
-> runs batch
-> reviews Postgres/Phoenix results
-> exports summary for threshold and label review
```

Credential policy:

- No Discord webhook.
- No Gemini key.
- No broad shell access required for ordinary runs.

## Proposed Architecture

Use two layers:

### Regression Runner

A repo-owned script should be the stable orchestration entry point for CI and local runs.

Responsibilities:

- read `scripts/regression_questions.jsonl`
- validate required fields
- select all cases, one case, or a category
- call an n8n regression webhook or run direct HTTP calls against services
- persist summary output
- exit with a CI-friendly status code

Initial recommendation:

Start with the n8n webhook path because it exercises the same workflow logic we are building for production.

### n8n Regression Workflow

Create a retrieval-only n8n workflow first.

Responsibilities:

- accept one case or a batch
- normalize the question
- call embedder
- query Qdrant
- call reranker
- apply dedupe
- assemble context
- classify retrieval outcome against expected behavior
- write run/result rows
- emit Phoenix spans

Add full-answer mode later by reusing the Phase 7 active-call path and disabling Discord posting by default.

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

### Step 3: Build Retrieval-Only Workflow

Create `workflows/n8n/rag-regression-phase-8-retrieval-only.json`.

Nodes:

- `Manual Trigger`
- `Set Regression Run Config`
- `Load Regression Cases`
- `Split In Batches`
- `Create Regression Run`
- `Prepare Case`
- `Normalize Query`
- `Emit Phoenix Case Started`
- `Query Embedding`
- `Qdrant Vector Search`
- `Rerank Candidates`
- `Dedupe And Context Decision`
- `Assemble Context Contract`
- `Classify Retrieval Outcome`
- `Write Regression Result`
- `Emit Phoenix Case Completed`
- `Finalize Regression Run`

This workflow must not include Gemini or Discord nodes.

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

- reuse the Phase 7 context/prompt flow
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
