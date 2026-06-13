# Phase 4: Stage 1 Retrieval Refusal Gate
**Status:** Draft
**Scope:** Make the Qdrant-stage refusal gate explicit, observable, and testable before reranking and dedupe.
**Related:** n8n Execution Plan, Retrieval Context Prompt Contracts, Phase 3B, Observability Design

## Purpose
Phase 4 implements only the first refusal gate from the retrieval contract:

```text
Qdrant top_k=20
-> filter retrieval_score >= 0.55
-> if fewer than 3 results pass, refuse
```

This is not the full retrieval quality system. Reranker refusal, dedupe effects, and final context/prompt refusal happen in later phases.

## Workflow Artifact
Import this file into repo-owned n8n:

```text
workflows/n8n/rag-active-call-phase-4-stage-1-retrieval-gate.json
```

Workflow name:

```text
RAG Active Call - Phase 4 Stage 1 Retrieval Gate
```

## What Changes From Phase 3B
Phase 3B already retrieved, assembled context, generated/refused, posted to Discord, finalized Postgres, and emitted Phoenix traces.

Phase 4 adds one explicit node after Qdrant:

```text
Qdrant Vector Search
-> Build Stage 1 Retrieval Gate
-> Build Retrieval And Context Decision
```

`Build Stage 1 Retrieval Gate` owns only these decisions:

| Case | Gate status | Gate reason | Result |
|---|---|---|---|
| Query embedding failed | `refused` | `query_embedding_failed` | Refuse |
| Qdrant query failed | `refused` | `qdrant_query_failed` | Refuse |
| Qdrant returned no candidates | `refused` | `no_qdrant_results` | Refuse |
| Fewer than 3 candidates pass `retrieval_score >= 0.55` | `refused` | `fewer_than_min_stage_1_results` | Refuse |
| At least 3 candidates pass | `passed` | `stage_1_retrieval_passed` | Continue |

## What Does Not Change Yet
Phase 4 does not add:

- CrossEncoder reranking
- reranker-score refusal
- message-overlap dedupe
- reaction boost
- final contract-perfect context formatting
- passive listener behavior

Those are intentionally later phases.

## Gemini Grounding Guard
Stage 1 is intentionally broad recall. Do not tighten `retrieval_score` just because a semantically adjacent query passes Stage 1.

Instead, Phase 4 hardens the Gemini prompt and response handling:

- Gemini is instructed to check whether context directly answers the specific company, role, interview, or topic in the user question.
- Semantic similarity alone is not enough to answer.
- If the context does not directly answer the question, Gemini must return the exact standard refusal.
- Every non-refusal answer must include at least one source citation.
- If Gemini returns a non-refusal answer with no valid citation, the workflow converts it to the standard refusal with `refusal_reason = gemini_uncited_answer`.

This is a temporary safety guard until Phase 5 reranking can catch more weak matches before Gemini.

## Observability
Phoenix should show:

- `qdrant.query_completed`
- `retrieval.stage1_gate_passed` or `retrieval.stage1_gate_refused`
- `context.assembled`, `context.overflow`, or `context.insufficient`

The Stage 1 gate span should include:

- `stage_1_gate_status`
- `stage_1_gate_reason`
- `stage_1_gate_threshold`
- `stage_1_gate_min_result_count`
- `raw_candidate_count`
- `passed_candidate_count`
- `phase_4_scope`

Postgres should keep durable transaction state and retrieval rows.

## Expected Success
For a Stage 1 pass:

```text
stage_1_gate_status = passed
stage_1_gate_reason = stage_1_retrieval_passed
passed_candidate_count >= 3
workflow continues to context assembly
```

For a Stage 1 refusal:

```text
stage_1_gate_status = refused
stage_1_gate_reason is populated
status = refused
retrieval_status = no_context
response_status = posted
Gemini is not called
```

## Test Cases
Run at least these tests before moving to Phase 5.

| Test | Setup | Expected Result |
|---|---|---|
| Missing collection | Set `qdrant_collection` to a fake collection | Refusal with `qdrant_query_failed` |
| No useful match | Use an out-of-domain/nonsense query | Refusal with `no_qdrant_results` or `fewer_than_min_stage_1_results` |
| Known answer candidate | Use a known indexed question | Stage 1 passes with at least 3 candidates |
| Current token-budget edge case | Use the Meta partnership interview question | Stage 1 should pass; later context budget may still refuse |
| Stage 1 false positive | Ask a semantically adjacent but unsupported question, such as "How is the culture at Coupang?" | Stage 1 may pass, Gemini may refuse with `gemini_context_insufficient`; this is expected until Phase 5 reranking |
| Uncited Gemini answer | Ask an unsupported question that causes Gemini to produce a generic answer | Workflow converts answer to standard refusal with `gemini_uncited_answer` |

If Gemini refuses after Stage 1 passed, the final transaction should use:

```text
status = refused
retrieval_status = no_context
refusal_reason = gemini_context_insufficient
```

This means Qdrant found candidate chunks, but the final grounding check found the assembled context insufficient for the actual question.

If Gemini produces an answer without any source citation, the final transaction should use:

```text
status = refused
retrieval_status = no_context
refusal_reason = gemini_uncited_answer
```

This means the workflow rejected an answer that did not meet the citation/grounding contract.

## Validation Queries
Latest transaction:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT transaction_id, status, retrieval_status, response_status,
       refusal_reason, failure_reason, query_hash, latency_ms, created_at
FROM rag_transactions
ORDER BY created_at DESC
LIMIT 5;"
```

Latest retrieval rows:

```bash
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT rank, retrieval_score, channel_name,
       payload->>'stage_1_gate_status' AS stage_1_gate_status,
       payload->>'stage_1_gate_reason' AS stage_1_gate_reason,
       left(payload->>'text', 160) AS text_preview
FROM rag_retrieval_results
WHERE transaction_id = (
  SELECT transaction_id
  FROM rag_transactions
  ORDER BY created_at DESC
  LIMIT 1
)
ORDER BY rank
LIMIT 10;"
```

Phoenix:

```powershell
ssh -i "$HOME\mykey.key" -L 6006:127.0.0.1:6006 ubuntu@discord-notifier.duckdns.org
```

Open:

```text
http://127.0.0.1:6006
```

Look for project:

```text
discord-rag-bot-phase-4
```

## Pass / Fail
Pass:

- Workflow runs end to end.
- Phoenix shows the Stage 1 gate span.
- Stage 1 pass/refusal is visible without opening every n8n node.
- Refusal does not call Gemini when Qdrant fails the gate.

Fail:

- Stage 1 refusal reason is missing or ambiguous.
- Phoenix trace lacks `retrieval.stage1_gate_*`.
- Postgres final status contradicts the gate decision.
- Workflow silently answers after fewer than 3 Stage 1 candidates pass.
