# Phase 1 n8n Implementation
**Status:** Draft
**Scope:** Import and test the minimum active-call transaction spine
**Related:** n8n Execution Plan, n8n Workflow Design, Observability Design

## Purpose
This workflow implements Phase 1 from the n8n execution plan: the minimum durable transaction spine.

It does not perform real embedding, vector search, reranking, Gemini generation, Discord response dispatch, passive listening, feedback correlation, weekly metrics, or alerting.

## Workflow Artifact
Import this file into n8n:

```text
workflows/n8n/rag-active-call-phase-1-transaction-spine.json
```

Workflow name:

```text
RAG Active Call - Phase 1 Transaction Spine
```

## What It Proves
The workflow proves that n8n can:

- simulate an active Discord bot call
- create a durable `rag_transactions` row
- write ingress and routing events to `rag_trace_events`
- normalize the query with the required `search_query:` prefix
- check whether the Qdrant collection `tpm_unite_history` exists
- write a retrieval readiness decision
- update final transaction status
- read back both the transaction and trace events

## Expected Outcomes
If Qdrant does not yet have the `tpm_unite_history` collection, the final transaction should show:

```text
status = refused
retrieval_status = no_context
response_status = not_posted
refusal_reason = qdrant_collection_missing
```

If Qdrant does have the `tpm_unite_history` collection, the final transaction should show:

```text
status = retrieving
retrieval_status = not_started
response_status = not_posted
refusal_reason = null
```

The second case means the Phase 1 transaction spine is ready for Phase 2 retrieval implementation.

## Required Credential
The workflow uses the n8n Postgres credential:

```text
Name: Postgres account
Server credential id: 91KXbNmHZPM5D5mO
```

If importing into a different n8n instance, reselect the Postgres credential on each Postgres node.

## Test Steps
1. Pull latest `main` on the Oracle server.
2. Import the workflow JSON into n8n.
3. Open the workflow.
4. Confirm every Postgres node has the `Postgres account` credential selected.
5. Execute the workflow manually.
6. Inspect `Read Final Transaction`.
7. Inspect `Read Trace Events`.

## Validation Query
Run this in the server shell to inspect the latest transaction:

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
Phase 1 passes when:

- the workflow completes without node errors
- one `rag_transactions` row is created
- the row has `route_type = active_call`
- the row has a `normalized_query` beginning with `search_query:`
- `rag_trace_events` contains:
  - `discord.event_received`
  - `routing.active_call`
  - `query.normalized`
  - `retrieval.collection_check`
  - `transaction.phase_1_completed`
- the final status matches the expected Qdrant collection state

## Known Non-Goals
This workflow intentionally does not:

- embed the query
- search Qdrant vectors
- retrieve chunks
- call Gemini
- post a Discord response
- emit Phoenix spans directly

Those are Phase 2 and later.
