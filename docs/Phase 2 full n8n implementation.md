# Phase 2 Full n8n Implementation
**Status:** Draft
**Scope:** Import and test the full active-call Phase 2 path
**Related:** n8n Execution Plan, n8n Workflow Design, Retrieval Context Prompt Contracts, Observability Design

## Purpose
This workflow implements the original Phase 2 target:

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

It can produce either one grounded response or one refusal, with transaction and trace evidence.

## Workflow Artifact
Import this file into n8n:

```text
workflows/n8n/rag-active-call-phase-2-full-happy-path.json
```

Workflow name:

```text
RAG Active Call - Phase 2 Full Happy Path
```

## Required Runtime Dependencies
This workflow needs more than the Phase 2 gate:

- Qdrant collection `tpm_unite_history` must exist and have points.
- A query embedding endpoint must exist at the value configured in `embedding_url`.
- The embedding endpoint must return a 768-dimension vector using `nomic-ai/nomic-embed-text-v1.5`.
- n8n must have `GEMINI_API_KEY` available in its environment.
- The `discord_webhook_url` field must be replaced with a real Discord webhook URL ending in `?wait=true`.

The default embedding URL is:

```text
http://ragbot-embedder:8000/embed
```

That service is not part of Phase 0 yet. It needs to be added before this workflow can execute the answer path.

## What It Proves
The workflow proves that n8n can:

- create and finalize a durable transaction
- normalize the query with `search_query:`
- call the query embedding runtime
- run Qdrant vector search
- store raw retrieval candidates in `rag_retrieval_results`
- refuse when retrieval is missing or weak
- assemble a structured context block from retrieved chunks
- call Gemini with the prompt/response contract
- post an answer or refusal to Discord
- store the Discord response message ID
- write trace events for the full active-call path

## Expected Answer Outcome
If retrieval finds enough context and Gemini succeeds, the final transaction should show:

```text
status = answered
retrieval_status = context_found
response_status = posted
refusal_reason = null
discord_response_message_id = <Discord message ID>
```

Trace events should include:

- `discord.event_received`
- `routing.active_call`
- `query.normalized`
- `qdrant.query_completed`
- `transaction.phase_2_completed`

`rag_retrieval_results` should contain up to 20 Qdrant candidates for the transaction.

## Expected Refusal Outcome
If embedding fails, Qdrant fails, no candidates are found, or fewer than 3 candidates pass `retrieval_score >= 0.55`, the final transaction should show:

```text
status = refused
retrieval_status = no_context
response_status = posted
refusal_reason = <reason>
discord_response_message_id = <Discord message ID>
```

The Discord response should be exactly:

```text
I don't have enough TPM Unite specific context to answer this confidently, try rephrasing or ask the community directly.
```

## Current Non-Goals
This workflow intentionally still excludes:

- passive listener behavior
- CrossEncoder reranking
- dedupe
- reaction boost
- feedback correlation
- weekly metrics
- advanced alerting

Those are later phases in the execution plan.
