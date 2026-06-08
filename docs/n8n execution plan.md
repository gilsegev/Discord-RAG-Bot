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

Phoenix should show the execution trace.

Postgres should store durable transaction state and key events.

Expected outcome:

When the workflow fails, the failure point is obvious without manually stepping through the entire n8n canvas.

## Phase 4: Retrieval Refusal Gate
Add refusal logic before adding more sophisticated retrieval behavior.

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

The bot refuses weak retrieval instead of generating unsupported answers.

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

## Phase 6: Dedupe Placeholder
Add message-overlap dedupe after reranking and before context assembly.

Initial rule:

```text
shared = intersection(chunk_a.message_ids, chunk_b.message_ids)
overlap_ratio = len(shared) / min(len(chunk_a.message_ids), len(chunk_b.message_ids))
```

If `overlap_ratio > 0.5`, keep the stronger chunk.

Ordering:

```text
rerank
-> reaction boost placeholder
-> dedupe by message_ids
-> context assembly
```

Expected outcome:

Repeated evidence is reduced before the LLM sees the final context.

Note:

Full reply-root dedupe can be added later when `root_message_id` is available in the Qdrant payload.

## Phase 7: Context Assembly And Prompt Contract
Implement the context block exactly from the retrieval/context/prompt contract.

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

## Phase 8: Passive Listener
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

## Phase 9: Feedback Correlation
Add Discord reaction monitoring after bot responses store `discord_response_message_id`.

Flow:

```text
reaction event
-> check whether target message is a bot response
-> look up transaction by discord_response_message_id
-> normalize feedback
-> upsert feedback row
-> update trace or metrics
```

Expected outcome:

User reactions can be tied back to the original retrieval and answer transaction.

## Phase 10: Weekly Metrics And Alerts
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
