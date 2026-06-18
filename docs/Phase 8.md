# Phase 8: Regression Evaluation Harness

## Status

Planning branch: `phase-8-regression-evaluation`

## Goal

Build a repeatable way to run curated regression questions through the active-call RAG pipeline before adding passive listener behavior.

The harness should make quality measurable across a question set instead of relying on one-off manual n8n runs.

## Why This Comes Before Passive Listener

Passive listening increases risk because the bot decides when to engage without a direct user request. Before enabling that behavior, the active-call pipeline needs repeatable regression evidence for:

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

## Initial Inputs

Expected seed files:

- `docs/regression_questions.jsonl`
- additional regression questions and instructions provided by altctrldeliver

## Required Modes

### Retrieval-Only Mode

Runs query normalization, embedding, Qdrant retrieval, Stage 1 gate, reranking, dedupe, and context assembly.

This mode skips Gemini so the team can evaluate retrieval quality without model variability or API cost.

### Full Answer Mode

Runs the full workflow through Gemini and Discord-safe response validation.

This mode evaluates answer quality, citation behavior, refusal behavior, and response length.

## Persisted Evidence

Each regression run should store:

- run ID
- workflow version
- question file version
- started/completed timestamps
- aggregate pass/fail counts
- notes or reviewer

Each question result should store:

- run ID
- question ID
- question text
- expected behavior/category
- actual transaction ID
- retrieval status
- final status
- refusal reason
- selected chunk IDs
- selected channel names
- retrieval and reranker scores
- context token estimate
- answer length
- citation status
- latency
- pass/fail/review-needed status
- Phoenix trace link or trace ID

## Success Criteria

- The known Meta partnership seed case runs through the harness.
- Retrieval-only mode can be run without Gemini.
- Full answer mode can be run when Gemini validation is needed.
- Results are inspectable from Postgres without opening n8n.
- Failures are categorized clearly enough for a developer to debug the pipeline stage that regressed.

## Open Design Questions

- Should regression results live in new tables or reuse `rag_eval_labels` plus transaction metadata?
- Should the n8n workflow own batch orchestration, or should a script call n8n per question?
- How should expected outcomes be encoded for multi-part questions?
- What pass/fail thresholds block moving to passive listener behavior?
