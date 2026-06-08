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

## Interface Glossary
This section defines the interfaces referenced by the node descriptions below.
| Interface | Meaning | Used For | Design Reference |
|-----------|---------|----------|------------------|
| Discord Gateway | Discord event stream received by the bot. | Incoming messages, mentions, passive channel traffic, and reaction events. | [Arch Overview: Discord Gateway](Arch%20overview.md#discord-gateway); [Data Extraction: Message Object Anatomy](Data%20Extraction%20Methodology.md#message-object-anatomy) |
| Discord API | Discord outbound API. | Posting bot answers, refusals, alerts, and metric digests. | [Arch Overview: End-to-End Data Flow](Arch%20overview.md#2-end-to-end-data-flow-and-observability-lifecycle) |
| Internal workflow state | The in-memory n8n item data passed between nodes in the same workflow execution. | Carrying normalized query text, route decisions, candidate chunks, scores, and temporary context between nodes. | [n8n Workflow: Primary Workflow](#primary-workflow-rag-answer-workflow) |
| Phoenix | Trace and span observability store. | Recording per-node execution, latency, retrieval evidence, refusal decisions, and trace links. | [Observability: Phoenix Transaction Trace Schema](Observability%20design.md#5a-phoenix-transaction-trace-schema) |
| Postgres | Durable operational data store. | Persisting transactions, feedback rows, weekly rollups, alert state, and Discord response IDs. | [Observability: Postgres Transaction Schema](Observability%20design.md#5b-postgres-transaction-schema) |
| Qdrant | Vector database. | Retrieving semantically relevant chunks from embedded Discord history. | [Retrieval Contracts: Retrieval Contract](retrieval-context-prompt-contracts.md#1-retrieval-contract); [Arch Overview: Qdrant Vector DB](Arch%20overview.md#qdrant-vector-db) |
| Nomic embedding runtime or service | Query embedding component using the same embedding model family as ingestion. | Converting user queries into vectors for Qdrant search. | [Retrieval Contracts: Embedding Model](retrieval-context-prompt-contracts.md#12-embedding-model) |
| CrossEncoder runtime | Reranking component. | Reordering retrieved chunks by query-specific relevance after Qdrant retrieval. | [Retrieval Contracts: Retrieval Stages And Score Definitions](retrieval-context-prompt-contracts.md#13-retrieval-stages-and-score-definitions) |
| Gemini API | LLM generation endpoint. | Generating grounded answers when retrieval quality is sufficient. | [Retrieval Contracts: Prompt / Response Contract](retrieval-context-prompt-contracts.md#3-prompt--response-contract); [Arch Overview: Gemini API](Arch%20overview.md#gemini-api) |
| Alerting | Operational notification path. | Warning maintainers about quality, latency, dispatch, or infrastructure failures. | [Alerting: Starting Alert Thresholds](Alerting.md#starting-alert-thresholds); [Observability: n8n Integration Points](Observability%20design.md#7-n8n-integration-points) |
| Weekly metrics workflow | Scheduled reporting workflow. | Publishing the weekly `#bot-metrics` digest from precomputed metrics. | [Observability: Weekly Metrics Digest](Observability%20design.md#9-weekly-metrics-digest) |
| GitHub issues | Team follow-up tracker. | Capturing critical quality failures or design/implementation gaps that need owner action. | [Alerting: Alert Implementation](Alerting.md#alert-implementation) |

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
             |                  |
          success             API/model failure
             |                  v
             |          [17a Generation Failure]
             |                  |
             |                  v
             |          [21 Finalize Transaction]
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
References:
- [Arch Overview: Discord Gateway](Arch%20overview.md#discord-gateway)
- [Data Extraction: Message Object Anatomy](Data%20Extraction%20Methodology.md#message-object-anatomy)

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
- Internal workflow state
References:
- [Arch Overview: Execution Tracking Stages](Arch%20overview.md#execution-tracking-stages)
- [Observability: Transaction Trace Schema](Observability%20design.md#5-transaction-trace-schema)
- [Observability: n8n Integration Points](Observability%20design.md#7-n8n-integration-points)

### 03 Route Event
Description:
Classifies the event as active, passive, or ignored.
What it does:
- detects direct bot mentions
- detects passive-listener candidates
- sends non-actionable events to ignored status
Interfaces:
- Internal workflow state
- Postgres
- Phoenix
References:
- [Arch Overview: Execution Tracking Stages](Arch%20overview.md#execution-tracking-stages)
- [Observability: Event And Span Taxonomy](Observability%20design.md#6-event-and-span-taxonomy)
- [Retrieval Contracts: Passive Listener Retrieval](retrieval-context-prompt-contracts.md#open-questions-for-the-team)

### 04 Active Call
Description:
Handles direct user requests to the bot.
What it does:
- bypasses passive relevance filtering
- moves directly to retrieval
- requires either a grounded answer or a clear refusal
Interfaces:
- Internal workflow state
- Phoenix
- Postgres
References:
- [Arch Overview: End-to-End Data Flow](Arch%20overview.md#2-end-to-end-data-flow-and-observability-lifecycle)
- [Arch Overview: Execution Tracking Stages](Arch%20overview.md#execution-tracking-stages)
- [Retrieval Contracts: Retrieval Contract](retrieval-context-prompt-contracts.md#1-retrieval-contract)

### 05 Passive Relevance Gate
Description:
Decides whether ordinary channel traffic should trigger retrieval.
What it does:
- checks lightweight heuristics such as question markers, length, or configured passive rules
- drops messages that should not trigger the bot
- rate-limits passive workload
Interfaces:
- Internal workflow state
- Postgres
- Phoenix
References:
- [Arch Overview: Execution Tracking Stages](Arch%20overview.md#execution-tracking-stages)
- [Observability: Event And Span Taxonomy](Observability%20design.md#6-event-and-span-taxonomy)
- [Retrieval Contracts: Open Questions](retrieval-context-prompt-contracts.md#open-questions-for-the-team)

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
References:
- [Arch Overview: Execution Tracking Stages](Arch%20overview.md#execution-tracking-stages)
- [Observability: Event And Span Taxonomy](Observability%20design.md#6-event-and-span-taxonomy)
- [Observability: Postgres Transaction Schema](Observability%20design.md#5b-postgres-transaction-schema)

### 07 Normalize Query
Description:
Prepares the user query for retrieval.
What it does:
- cleans bot mentions
- preserves the user question
- applies the Nomic `search_query:` prefix
- extracts optional channel/date/thread filters if present
Interfaces:
- Internal workflow state
- Phoenix
References:
- [Retrieval Contracts: Query Text Format](retrieval-context-prompt-contracts.md#11-query-text-format)
- [Retrieval Contracts: Default Search Scope](retrieval-context-prompt-contracts.md#14-default-search-scope)
- [Observability: n8n Integration Points](Observability%20design.md#7-n8n-integration-points)

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
References:
- [Retrieval Contracts: Embedding Model](retrieval-context-prompt-contracts.md#12-embedding-model)
- [Retrieval Contracts: Query Text Format](retrieval-context-prompt-contracts.md#11-query-text-format)
- [Observability: Runtime SLO And Concurrency Policy](Observability%20design.md#runtime-slo-and-concurrency-policy)

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
References:
- [Retrieval Contracts: Retrieval Contract](retrieval-context-prompt-contracts.md#1-retrieval-contract)
- [Retrieval Contracts: Retrieval Stages And Score Definitions](retrieval-context-prompt-contracts.md#13-retrieval-stages-and-score-definitions)
- [Observability: Postgres Transaction Schema](Observability%20design.md#5b-postgres-transaction-schema)

### 10 Retrieval Quality Gate
Description:
Decides whether Qdrant found enough usable context.
What it does:
- applies retrieval score threshold
- checks minimum result count
- triggers refusal if context is missing or too weak
Interfaces:
- Internal workflow state
- Phoenix
- Postgres
- Alerting
References:
- [Retrieval Contracts: Retrieval Stages And Score Definitions](retrieval-context-prompt-contracts.md#13-retrieval-stages-and-score-definitions)
- [Retrieval Contracts: Uncertainty Handling](retrieval-context-prompt-contracts.md#34-uncertainty-handling)
- [Alerting: Refusal And Quality Alerts](Alerting.md#refusal-and-quality-alerts)

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
References:
- [Retrieval Contracts: Retrieval Stages And Score Definitions](retrieval-context-prompt-contracts.md#13-retrieval-stages-and-score-definitions)
- [Observability: Runtime SLO And Concurrency Policy](Observability%20design.md#runtime-slo-and-concurrency-policy)
- [Observability: Transaction Trace Schema](Observability%20design.md#5-transaction-trace-schema)

### 12 Rerank Quality Gate
Description:
Checks whether reranked context is relevant enough.
What it does:
- applies the reranker threshold
- flags weak-signal results
- triggers refusal if all reranked context is too weak
Interfaces:
- Internal workflow state
- Phoenix
- Postgres
- Alerting
References:
- [Retrieval Contracts: Retrieval Stages And Score Definitions](retrieval-context-prompt-contracts.md#13-retrieval-stages-and-score-definitions)
- [Retrieval Contracts: Uncertainty Handling](retrieval-context-prompt-contracts.md#34-uncertainty-handling)
- [Alerting: Refusal And Quality Alerts](Alerting.md#refusal-and-quality-alerts)

### 13 Dedupe Chunks
Description:
Removes repeated or overlapping evidence before the LLM sees it.
What it does:
- compares retrieved chunk `message_ids`
- drops highly overlapping chunks
- keeps higher-scoring evidence
- records kept and dropped chunk IDs
Interfaces:
- Internal workflow state
- Phoenix
- Postgres
References:
- [Retrieval Contracts: Dedupe Strategy For Overlapping Chunks](retrieval-context-prompt-contracts.md#15-dedupe-strategy-for-overlapping-chunks)
- [Retrieval Contracts: Context Assembly Contract](retrieval-context-prompt-contracts.md#2-context-assembly-contract)
- [Observability: Postgres Transaction Schema](Observability%20design.md#5b-postgres-transaction-schema)

### 14 Assemble Context
Description:
Builds the structured context block sent to Gemini.
What it does:
- formats top chunks with channel, thread, date range, score, message IDs, and Discord link
- preserves citations
- estimates token usage
Interfaces:
- Internal workflow state
- Phoenix
- Postgres
References:
- [Retrieval Contracts: What Gets Sent To The LLM](retrieval-context-prompt-contracts.md#21-what-gets-sent-to-the-llm)
- [Retrieval Contracts: Token Budget](retrieval-context-prompt-contracts.md#22-token-budget)
- [Retrieval Contracts: Source And Citation Style](retrieval-context-prompt-contracts.md#33-source-and-citation-style)

### 15 Context Sufficiency Gate
Description:
Checks whether final context is enough to answer.
What it does:
- enforces context token budget
- refuses if too few chunks remain after dedupe or trimming
- logs context insufficiency separately from retrieval failure
Interfaces:
- Internal workflow state
- Phoenix
- Postgres
- Alerting
References:
- [Retrieval Contracts: Context Overflow Handling](retrieval-context-prompt-contracts.md#23-context-overflow-handling)
- [Retrieval Contracts: Uncertainty Handling](retrieval-context-prompt-contracts.md#34-uncertainty-handling)
- [Alerting: Refusal And Quality Alerts](Alerting.md#refusal-and-quality-alerts)

### 16 Build Prompt
Description:
Creates the final system and user prompt for Gemini.
What it does:
- applies the prompt/response contract
- inserts assembled context
- includes refusal and citation rules
Interfaces:
- Gemini API
- Phoenix
References:
- [Retrieval Contracts: Prompt / Response Contract](retrieval-context-prompt-contracts.md#3-prompt--response-contract)
- [Retrieval Contracts: System Prompt](retrieval-context-prompt-contracts.md#31-system-prompt)
- [Retrieval Contracts: Source And Citation Style](retrieval-context-prompt-contracts.md#33-source-and-citation-style)

### 17 Gemini Generation
Description:
Calls Gemini to generate the response.
What it does:
- sends the prompt and context
- captures model latency
- returns either a grounded answer or refusal text when the API call succeeds
- treats Gemini API, authentication, model, timeout, and malformed-response failures as operational failures, not retrieval refusals
Interfaces:
- Gemini API
- Phoenix
- Postgres
References:
- [Arch Overview: Gemini API](Arch%20overview.md#gemini-api)
- [Retrieval Contracts: Prompt / Response Contract](retrieval-context-prompt-contracts.md#3-prompt--response-contract)
- [Observability: Runtime SLO And Concurrency Policy](Observability%20design.md#runtime-slo-and-concurrency-policy)

### 17a Generation Failure
Description:
Handles Gemini API or response failures before Discord dispatch.
What it does:
- catches non-2xx Gemini responses, unsupported model errors, authentication errors, timeouts, and missing response text
- records `status = failed`, `response_status = failed`, and a failure reason such as `gemini_api_failed`, `gemini_model_not_found`, or `gemini_malformed_response`
- logs the Gemini status code and sanitized error summary to observability
- does not post the standard retrieval-refusal text, because the system did find context but failed during generation
Interfaces:
- Gemini API
- Phoenix
- Postgres
- Alerting
References:
- [Observability: Event And Span Taxonomy](Observability%20design.md#6-event-and-span-taxonomy)
- [Observability: n8n Integration Points](Observability%20design.md#7-n8n-integration-points)
- [Alerting: Critical Alerts](Alerting.md#critical-alerts)

### 18 Refusal Response
Description:
Builds the standard refusal response when context is missing or weak.
What it does:
- uses the exact refusal text from the prompt contract
- records `refusal_reason`
- avoids calling Gemini when refusal is already determined by retrieval quality
Interfaces:
- Discord API
- Phoenix
- Postgres
References:
- [Retrieval Contracts: Refusal Text](retrieval-context-prompt-contracts.md#32-refusal-text)
- [Retrieval Contracts: Uncertainty Handling](retrieval-context-prompt-contracts.md#34-uncertainty-handling)
- [Observability: Refusal Quality Metrics](Observability%20design.md#refusal-quality-metrics)

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
References:
- [Retrieval Contracts: Prompt / Response Contract](retrieval-context-prompt-contracts.md#3-prompt--response-contract)
- [Retrieval Contracts: Safety Rule](retrieval-context-prompt-contracts.md#38-safety-rule)
- [Observability: System Observability Goals](Observability%20design.md#3-system-observability-goals)

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
References:
- [Arch Overview: End-to-End Data Flow](Arch%20overview.md#2-end-to-end-data-flow-and-observability-lifecycle)
- [Observability: Postgres Transaction Schema](Observability%20design.md#5b-postgres-transaction-schema)
- [Alerting: Alert Implementation](Alerting.md#alert-implementation)

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
References:
- [Arch Overview: Dispatch Verification](Arch%20overview.md#execution-tracking-stages)
- [Observability: Transaction Trace Schema](Observability%20design.md#5-transaction-trace-schema)
- [Observability: Weekly Metrics Digest](Observability%20design.md#9-weekly-metrics-digest)

## Supporting Workflow: Feedback Correlation Workflow (phase 2)
```text
[01 Discord Feedback Ingress]
        |
        v
[02 Is Bot Response?]
        |
        +--------------------+
        |                    |
      yes                   no
        |                    v
        |             [03 Mark Unmatched]
        v
[04 Lookup Transaction]
        |
        +--------------------+
        |                    |
      found              not found
        |                    v
        |             [03 Mark Unmatched]
        v
[05 Normalize Feedback]
        |
        v
[06 Upsert Feedback]
        |
        v
[07 Update Trace / Metrics]
```

Purpose:
Connect Discord reactions or explicit feedback back to the original bot transaction.
Key dependency:
The primary answer workflow must store `discord_response_message_id` in Postgres when node `20 Discord Response` posts the bot reply. Feedback correlation uses that field to map:

```text
reaction target message -> discord_response_message_id -> transaction_id
```

What it does:
- monitors Discord reaction events on bot responses
- supports explicit feedback events when available
- counts only configured feedback reactions in v1, such as thumbs-up and thumbs-down
- hashes or redacts `feedback_author_id`
- writes one feedback row per user per bot response
- treats add/remove or repeated feedback as last-write-wins
- logs unmatched feedback events without including them in weekly metrics
Interfaces:
- Discord Gateway
- Postgres
- Phoenix
- Weekly metrics workflow
References:
- [Arch Overview: Feedback Loop And Performance Grading](Arch%20overview.md#3-feedback-loop-and-performance-grading)
- [Observability: rag_feedback](Observability%20design.md#rag_feedback)
- [Observability: Phase 3 Feedback Correlation](Observability%20design.md#phase-3-feedback-correlation)
Current status:
This workflow now has the expected high-level shape, but the detailed feedback/reaction design is still open. Issue #4 tracks the remaining decisions: exact Discord events, supported reaction set, explicit feedback UX, unmatched handling details, and whether critical feedback should open GitHub issues.

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
- Phoenix
- Discord API
References:
- [Observability: Weekly Metrics Digest](Observability%20design.md#9-weekly-metrics-digest)
- [Observability: rag_weekly_metrics](Observability%20design.md#rag_weekly_metrics)
- [Alerting: Info Alerts](Alerting.md#info-alerts)

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
- Postgres
- Phoenix
- Discord API
- GitHub issues
References:
- [Alerting: Starting Alert Thresholds](Alerting.md#starting-alert-thresholds)
- [Alerting: Alert Implementation](Alerting.md#alert-implementation)
- [Observability: Minimum Useful Views And Access Paths](Observability%20design.md#minimum-useful-views-and-access-paths)

## Open Questions
- Should active and passive events share one n8n workflow or be split after ingress?
- Where should query embedding run: inside n8n, as a local Python service, or as a small API?
- Where should the CrossEncoder reranker run: inside n8n, as a local service, or as a small API?
- Should retrieval refusals bypass Gemini entirely, or should Gemini format all final responses?
- How much prompt/context text should be sent to Phoenix by default?
- What exact passive-listener heuristics should be used in v1?
- What Discord channel should receive operational alerts?
- When `root_message_id` becomes available, how should dedupe rule 2 be updated?
