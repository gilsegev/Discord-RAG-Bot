# n8n Workflow Design

**Status:** Draft for team review  
**Scope:** High-level workflow design, not an implementation guide  
**Related:** Arch Overview, Retrieval Context Prompt Contracts, Observability Design, Alerting

## Purpose

This document explains how the TPM Unite RAG Bot should work inside n8n at a high level.

It is meant to help contributors understand:

- the end-to-end bot flow
- the major n8n nodes
- what each node does
- which systems each node talks to
- where retrieval, refusal, observability, and feedback fit

It does not define exact node configuration, credentials, SQL, prompts, or API payloads yet.

## Design Principles

- n8n is the orchestration layer.
- Qdrant is the semantic memory layer.
- Gemini is the generation layer.
- Phoenix stores trace-level evidence.
- Postgres stores durable transaction, feedback, and weekly reporting data.
- The bot should refuse when retrieval quality is weak.
- The main active-call path should target p95 latency under 5 seconds.
- Passive listener work should be rate-limited or sampled.

## Workflow Overview

The system has one primary answer workflow and three supporting workflows.

Primary workflow:

- `RAG Answer Workflow`

Supporting workflows:

- `Feedback Correlation Workflow`
- `Weekly Metrics Workflow`
- `Alerting Workflow`

## Primary Workflow: RAG Answer Workflow

```text
[01 Discord Ingress]
        |
        v
[02 Create Transaction]
        |
        v
[03 Route Event]
        |
        +--------------------------+
        |                          |
        v                          v
[04 Active Call]           [05 Passive Relevance Gate]
        |                          |
        |                    ignored? -> [06 Mark Ignored] -> [End]
        |                          |
        +------------+-------------+
                     |
                     v
          [07 Normalize Query]
                     |
                     v
          [08 Query Embedding]
                     |
                     v
          [09 Qdrant Retrieval]
                     |
                     v
          [10 Retrieval Quality Gate]
             |                  |
          pass               refuse
             |                  v
             |          [18 Refusal Response]
             v
          [11 Rerank Candidates]
                     |
                     v
          [12 Rerank Quality Gate]
             |                  |
          pass               refuse
             |                  v
             |          [18 Refusal Response]
             v
          [13 Dedupe Chunks]
                     |
                     v
          [14 Assemble Context]
                     |
                     v
          [15 Context Sufficiency Gate]
             |                  |
          pass               refuse
             |                  v
             |          [18 Refusal Response]
             v
          [16 Build Prompt]
                     |
                     v
          [17 Gemini Generation]
                     |
                     v
          [19 Response Policy Check]
                     |
                     v
          [20 Discord Response]
                     |
                     v
          [21 Finalize Transaction]
```

Observability runs throughout the workflow. Each major node emits trace events to Phoenix and key durable events to Postgres.

## Primary Workflow Nodes

### 01 Discord Ingress

Description:

Receives Discord message events.

What it does:

- accepts active bot mentions
- accepts passive channel messages
- captures message ID, channel, author, timestamp, and content

Interfaces:

- Discord Gateway
- Phoenix
- Postgres

### 02 Create Transaction

Description:

Creates one `transaction_id` for the event.

What it does:

- starts a trace
- writes the initial `rag_transactions` row
- records the raw event summary

Interfaces:

- Phoenix
- Postgres

### 03 Route Event

Description:

Classifies the event as active, passive, or ignored.

What it does:

- detects direct bot mentions
- detects passive-listener candidates
- sends non-actionable events to ignored status

Interfaces:

- Postgres
- Phoenix

### 04 Active Call

Description:

Handles direct user requests to the bot.

What it does:

- bypasses passive relevance filtering
- moves directly to retrieval
- requires either a grounded answer or a clear refusal

Interfaces:

- n8n workflow state
- Phoenix
- Postgres

### 05 Passive Relevance Gate

Description:

Decides whether ordinary channel traffic should trigger retrieval.

What it does:

- checks lightweight heuristics such as question markers, length, or configured passive rules
- drops messages that should not trigger the bot
- rate-limits passive workload

Interfaces:

- n8n workflow state
- Postgres
- Phoenix

### 06 Mark Ignored

Description:

Ends a passive event without response.

What it does:

- updates transaction status to `ignored`
- logs the route decision
- sends no Discord response

Interfaces:

- Postgres
- Phoenix

### 07 Normalize Query

Description:

Prepares the user query for retrieval.

What it does:

- cleans bot mentions
- preserves the user question
- applies the Nomic `search_query:` prefix
- extracts optional channel/date/thread filters if present

Interfaces:

- n8n workflow state
- Phoenix

### 08 Query Embedding

Description:

Converts the normalized query into a vector for Qdrant search.

What it does:

- uses the same Nomic Embed v1.5 query format as the retrieval contract
- embeds `search_query: <user question text>`
- records query embedding latency

Interfaces:

- Nomic embedding runtime or service
- Phoenix
- Postgres

### 09 Qdrant Retrieval

Description:

Queries the vector database for candidate chunks.

What it does:

- sends the embedded query to Qdrant
- retrieves top candidate chunks
- records `retrieval_score`, payload metadata, and latency

Interfaces:

- Qdrant
- Phoenix
- Postgres

### 10 Retrieval Quality Gate

Description:

Decides whether Qdrant found enough usable context.

What it does:

- applies retrieval score threshold
- checks minimum result count
- triggers refusal if context is missing or too weak

Interfaces:

- n8n workflow state
- Phoenix
- Postgres
- Alerting

### 11 Rerank Candidates

Description:

Reranks Qdrant candidates using a CrossEncoder reranker.

What it does:

- reranks retrieved chunks
- records `reranker_score`
- keeps the strongest candidates for dedupe and context assembly

Interfaces:

- CrossEncoder runtime
- Phoenix
- Postgres

### 12 Rerank Quality Gate

Description:

Checks whether reranked context is relevant enough.

What it does:

- applies the reranker threshold
- flags weak-signal results
- triggers refusal if all reranked context is too weak

Interfaces:

- n8n workflow state
- Phoenix
- Postgres
- Alerting

### 13 Dedupe Chunks

Description:

Removes repeated or overlapping evidence before the LLM sees it.

What it does:

- compares retrieved chunk `message_ids`
- drops highly overlapping chunks
- keeps higher-scoring evidence
- records kept and dropped chunk IDs

Interfaces:

- n8n workflow state
- Phoenix
- Postgres

### 14 Assemble Context

Description:

Builds the structured context block sent to Gemini.

What it does:

- formats top chunks with channel, thread, date range, score, message IDs, and Discord link
- preserves citations
- estimates token usage

Interfaces:

- n8n workflow state
- Phoenix
- Postgres

### 15 Context Sufficiency Gate

Description:

Checks whether final context is enough to answer.

What it does:

- enforces context token budget
- refuses if too few chunks remain after dedupe or trimming
- logs context insufficiency separately from retrieval failure

Interfaces:

- n8n workflow state
- Phoenix
- Postgres
- Alerting

### 16 Build Prompt

Description:

Creates the final system and user prompt for Gemini.

What it does:

- applies the prompt/response contract
- inserts assembled context
- includes refusal and citation rules

Interfaces:

- Gemini
- Phoenix

### 17 Gemini Generation

Description:

Calls Gemini to generate the response.

What it does:

- sends the prompt and context
- captures model latency
- returns either a grounded answer or refusal text

Interfaces:

- Gemini API
- Phoenix
- Postgres

### 18 Refusal Response

Description:

Builds the standard refusal response when context is missing or weak.

What it does:

- uses the exact refusal text from the prompt contract
- records `refusal_reason`
- avoids calling Gemini when refusal is already determined by retrieval quality

Interfaces:

- Discord
- Phoenix
- Postgres

### 19 Response Policy Check

Description:

Performs a lightweight final check before dispatch.

What it does:

- verifies response path is answer or refusal
- records whether citations are present for answered responses
- logs grounding/refusal status for later review

Interfaces:

- Phoenix
- Postgres

### 20 Discord Response

Description:

Sends the bot response back to Discord.

What it does:

- posts answer or refusal
- captures `discord_response_message_id`
- handles dispatch failure

Interfaces:

- Discord API
- Phoenix
- Postgres
- Alerting

### 21 Finalize Transaction

Description:

Closes the transaction.

What it does:

- writes final status
- writes completion time
- links response message ID
- marks the trace complete

Interfaces:

- Phoenix
- Postgres

## Supporting Workflow: Feedback Correlation Workflow

```text
[Feedback Event]
      |
      v
[Lookup Bot Response Message]
      |
      v
[Find Transaction]
      |
      v
[Write Feedback]
      |
      v
[Update Trace / Metrics]
```

Purpose:

Connect Discord reactions or explicit feedback back to the original bot transaction.

Interfaces:

- Discord reaction or feedback events
- Postgres `rag_feedback`
- Phoenix trace links
- Weekly metrics workflow

Current status:

This workflow still needs a concrete design. Issue #4 tracks the feedback/reaction correlation design gap.

## Supporting Workflow: Weekly Metrics Workflow

```text
[Scheduled Trigger]
      |
      v
[Read Postgres Metrics Sources]
      |
      v
[Compute Weekly Rollup]
      |
      v
[Upsert rag_weekly_metrics]
      |
      v
[Post #bot-metrics Digest]
```

Purpose:

Produce the weekly `#bot-metrics` digest without manual query assembly.

Interfaces:

- Postgres
- Phoenix eval outputs or trace links
- Discord `#bot-metrics`

## Supporting Workflow: Alerting Workflow

```text
[Scheduled / Inline Check]
      |
      v
[Evaluate Alert Thresholds]
      |
      v
[Post Warning or Critical Alert]
      |
      v
[Link Trace or Create Issue]
```

Purpose:

Detect operational or quality failures that need action.

Interfaces:

- Postgres rollups and transaction rows
- Phoenix trace links
- Discord ops channel
- GitHub issues for critical quality failures

## Open Questions

- Should active and passive events share one n8n workflow or be split after ingress?
- Where should query embedding run: inside n8n, as a local Python service, or as a small API?
- Where should the CrossEncoder reranker run: inside n8n, as a local service, or as a small API?
- Should retrieval refusals bypass Gemini entirely, or should Gemini format all final responses?
- How much prompt/context text should be sent to Phoenix by default?
- What exact passive-listener heuristics should be used in v1?
- What Discord channel should receive operational alerts?
- When `root_message_id` becomes available, how should dedupe rule 2 be updated?
