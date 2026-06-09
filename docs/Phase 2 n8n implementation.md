# Phase 2 n8n Implementation
**Status:** Draft
**Scope:** Import and test the active-call retrieval gate
**Related:** n8n Execution Plan, Architecture Overview, Observability Design

## Purpose
This workflow implements the next narrow step after the Phase 1 transaction spine.

It proves that n8n can check whether Qdrant is ready for retrieval before we add query embedding, vector search, Gemini generation, Discord response dispatch, reranking, dedupe, feedback, or weekly metrics.

## Why This Phase Is A Gate
The full Phase 2 happy path requires two things that are separate from n8n orchestration:

- Qdrant must contain the `tpm_unite_history` collection.
- n8n must have a query embedding path that produces vectors in the same embedding space as ingestion.

Without both, a Qdrant search would either fail or return meaningless results. This workflow therefore validates the retrieval preconditions first.

## Workflow Artifact
Import this file into n8n:

```text
workflows/n8n/rag-active-call-phase-2-retrieval-gate.json
```

Workflow name:

```text
RAG Active Call - Phase 2 Retrieval Gate
```

## What It Proves
The workflow proves that n8n can:

- simulate an active Discord bot call
- create a durable `rag_transactions` row
- write ingress and routing events to `rag_trace_events`
- normalize the query with the required `search_query:` prefix
- check the Qdrant collection used by retrieval
- inspect whether the collection has points
- write a retrieval gate decision
- finalize the transaction with an inspectable status

## Expected Outcomes
If Qdrant does not have the `tpm_unite_history` collection, the final transaction should show:

```text
status = refused
retrieval_status = no_context
response_status = not_posted
refusal_reason = qdrant_collection_missing
```

If Qdrant has the collection but it has no points, the final transaction should show:

```text
status = refused
retrieval_status = no_context
response_status = not_posted
refusal_reason = qdrant_collection_empty
```

If Qdrant has a populated collection, the final transaction should show:

```text
status = retrieving
retrieval_status = not_started
response_status = not_posted
refusal_reason = null
```

That third outcome means the stack is ready for the next increment: query embedding and real vector search.

## Required Credential
The workflow uses the n8n Postgres credential:

```text
Name: Postgres account
Server credential id: 91KXbNmHZPM5D5mO
```

If importing into a different n8n instance, reselect the Postgres credential on every Postgres node.

## Test Steps
1. Pull the latest PR branch on the Oracle server.
2. Import the workflow JSON into n8n.
3. Open the workflow.
4. Confirm every Postgres node has the `Postgres account` credential selected.
5. Execute the workflow manually.
6. Inspect `Read Final Transaction`.
7. Inspect `Read Trace Events`.

## Validation Queries
Run this in the server shell to inspect Qdrant:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
curl -s http://127.0.0.1:6333/collections/tpm_unite_history
```

Run this to inspect the latest transaction:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT transaction_id, status, retrieval_status, response_status,
       refusal_reason, normalized_query, created_at, completed_at
FROM rag_transactions
ORDER BY created_at DESC
LIMIT 5;"
```

Run this to inspect the latest trace events:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose exec postgres psql -U ragbot_admin -d ragbot -c "
SELECT event_name, node_name, status, event_payload, created_at
FROM rag_trace_events
ORDER BY created_at DESC
LIMIT 10;"
```

## Pass Criteria
Phase 2 retrieval gate passes when:

- the workflow completes without node errors
- one `rag_transactions` row is created
- the row has `route_type = active_call`
- the row has a `normalized_query` beginning with `search_query:`
- `rag_trace_events` contains:
  - `discord.event_received`
  - `routing.active_call`
  - `query.normalized`
  - `qdrant.collection_check`
  - `transaction.phase_2_gate_completed`
- the final status correctly reflects the Qdrant collection state

## Known Non-Goals
This workflow intentionally does not:

- embed the query
- search Qdrant vectors
- store rows in `rag_retrieval_results`
- assemble final context
- call Gemini
- post a Discord response
- emit Phoenix spans directly

Those are the next retrieval and generation increments.
