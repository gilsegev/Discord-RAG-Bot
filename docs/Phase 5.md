# Phase 5: CrossEncoder Reranker
**Status:** Draft
**Scope:** Add Stage 2 reranking after Qdrant Stage 1 retrieval and before context assembly.
**Related:** Retrieval Context Prompt Contracts, Phase 4, Observability Design, n8n Execution Plan

## Purpose
Phase 5 implements the second retrieval stage from the contract:

```text
Qdrant top_k=20
-> Stage 1 filter retrieval_score >= 0.55
-> CrossEncoder rerank
-> require at least 3 candidates with reranker_score > 0
-> select top 5 by reranker_score for context
```

Stage 1 remains broad recall. The CrossEncoder is the first stronger relevance check.

## Workflow Artifact
Import or push this workflow:

```text
workflows/n8n/rag-active-call-phase-5-reranker.json
```

Workflow name:

```text
RAG Active Call - Phase 5 Reranker
```

## Runtime Service
Phase 5 adds a repo-owned reranker service:

```text
http://reranker:8002/rerank
```

Model:

```text
cross-encoder/ms-marco-MiniLM-L-6-v2
```

The service receives:

```json
{
  "query": "user question",
  "candidates": [
    {"id": "qdrant-point-id", "text": "chunk text", "metadata": {}}
  ]
}
```

It returns candidates sorted by `reranker_score`.

## n8n Flow Change
Phase 5 inserts two nodes between Stage 1 and context assembly:

```text
Build Stage 1 Retrieval Gate
-> Prepare Reranker Request
-> CrossEncoder Rerank
-> Build Retrieval And Context Decision
```

`Build Retrieval And Context Decision` now uses `reranker_score`, not `retrieval_score`, to:

- rank context candidates
- choose top 5
- drop lowest-score chunks when over token budget
- populate the context block score field

## Refusal Rules
Refuse before Gemini when:

| Case | Refusal reason |
|---|---|
| Reranker service fails | `reranker_failed` |
| Reranker returns no usable results | `reranker_no_results` |
| Fewer than 3 candidates have `reranker_score > 0` | `reranker_score_below_threshold` |
| Context budget leaves fewer than 3 chunks | `context_token_budget_insufficient` |

If all selected chunks have `0 < reranker_score < 2`, add the weak-signal note before the answer.

## Observability
Phoenix should show:

- `qdrant.query_completed`
- `retrieval.stage1_gate_passed` or `retrieval.stage1_gate_refused`
- `rerank.completed` or `rerank.low_confidence`
- `context.assembled`, `context.overflow`, or `context.insufficient`

The rerank span should include:

- `reranker_model`
- `reranker_status_code`
- `reranker_score_threshold`
- `weak_reranker_score_threshold`
- `reranker_candidate_count`
- `reranker_passed_count`
- `latency_ms`
- `service_latency_ms`
- `refusal_reason`

Postgres `rag_retrieval_results` should store both:

- `retrieval_score`
- `reranker_score`

## Setup On Server
Pull the branch:

```bash
cd ~/Discord-RAG-Bot
git fetch origin
git checkout phase-5-reranker
git pull --ff-only origin phase-5-reranker
```

Add the new env var if missing:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
grep -q '^RERANKER_PORT=' .env || echo 'RERANKER_PORT=8002' >> .env
```

Apply the schema migration:

```bash
docker compose exec -T postgres psql -U ragbot_admin -d ragbot < sql/03-reranker-phase5-migration.sql
```

Start the reranker:

```bash
docker compose up -d --build reranker
```

Verify health from n8n:

```bash
docker compose exec n8n node -e "fetch('http://reranker:8002/health').then(r=>console.log('reranker', r.status)).catch(e=>{console.error(e.message);process.exit(1)})"
```

## Push Workflow To n8n
From the local repo with the n8n tunnel open:

```bash
npm run n8n:push -- workflows/n8n/rag-active-call-phase-5-reranker.json
```

## Test Cases
| Test | Query | Expected |
|---|---|---|
| Known good | `How does the partnership interview at Meta work, and do I need technical examples for it?` | Stage 1 passes, reranker passes, answer or valid grounded refusal |
| Weak match | `How is the culture at Coupang?` | Stage 1 may pass, reranker should refuse with `reranker_score_below_threshold` or Gemini guard refuses |
| Missing collection | fake `qdrant_collection` | Refuse before reranker |

## Validation Queries
Latest transaction:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT transaction_id, status, retrieval_status, response_status,
       refusal_reason, failure_reason, latency_ms, created_at
FROM rag_transactions
ORDER BY created_at DESC
LIMIT 5;"
```

Latest retrieval rows:

```bash
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT rank, retrieval_score, reranker_score, channel_name,
       payload->>'rank_after' AS rank_after,
       left(payload->>'text', 160) AS text_preview
FROM rag_retrieval_results
WHERE transaction_id = (
  SELECT transaction_id
  FROM rag_transactions
  ORDER BY created_at DESC
  LIMIT 1
)
ORDER BY COALESCE((payload->>'rank_after')::int, rank)
LIMIT 10;"
```

## Pass / Fail
Pass:

- workflow reaches the reranker service
- Postgres stores `reranker_score`
- Phoenix shows `rerank.completed` or `rerank.low_confidence`
- weak matches no longer become uncited answers

Fail:

- reranker scores are missing
- context block still displays Qdrant `retrieval_score`
- weak match answers without citation/grounding
- Phoenix has no rerank span
