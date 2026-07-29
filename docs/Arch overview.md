# Architecture Overview: Community Knowledge RAG Bot

This document outlines the high-level architecture, component interactions, and hosting environment for the community Discord RAG Bot project. It serves as an end-to-end framework for engineering volunteers to guide concrete design, schema definition, and implementation.

## 1. System Components and Responsibility Matrix
The system relies on a lean, containerized stack designed to balance low operational overhead with flexible, event-driven orchestration and comprehensive runtime tracking.

### Discord Gateway
**Role:** Event source and user interface.  
**Responsibility:** Provides real-time messaging data and receives bot responses.
**Implementation:** A persistent WebSocket connection receives `MESSAGE_CREATE`
and reaction events. For output, n8n calls Discord's authenticated Bot API
directly with the originating `channel_id`; channel-bound webhooks are not used
for production response dispatch.

Discord response dispatch uses:

```text
POST https://discord.com/api/v10/channels/{channel_id}/messages
Authorization: Bot <DISCORD_BOT_TOKEN>
```

The request replies to the originating `discord_message_id`. Discord's returned
message ID is stored as `discord_response_message_id` for delivery verification
and reaction-feedback correlation. The bot token is injected into n8n from the
server environment and is never stored in workflow JSON.

### n8n Orchestrator
**Role:** Intake router, execution state machine, dual-ingress filtering engine for active and passive events, context assembly layer, LLM coordinator, and telemetry dispatcher.
**Responsibility:** Handles request intake, mode routing, filtering, shared RAG execution, context assembly, LLM coordination, and telemetry dispatch.
**Implementation:** Advanced AI Workflow Canvas hosted on Oracle Cloud.

n8n should use a shared execution shape rather than separate copies of the RAG path. Active calls, passive candidates, manual regression runs, CI regression runs, and evaluator runs enter through a common intake/routing contract. That intake layer sets mode flags such as `trigger_source`, `run_mode`, `response_mode`, `allow_gemini`, and `allow_discord_post`, then calls the shared RAG core for retrieval, reranking, dedupe, context assembly, and optional generation.

### Qdrant Vector DB
**Role:** Semantic memory layer.  
**Responsibility:** Stores historical message embeddings for RAG lookups.  
**Implementation:** Runs natively in Docker with ARM64 compatibility.

### Local Embedder Service
**Role:** Query embedding runtime.  
**Responsibility:** Converts normalized user queries into vectors using `nomic-ai/nomic-embed-text-v1.5`.  
**Implementation:** Local FastAPI service called by n8n at `http://embedder:8000/embed`.

n8n owns the workflow node and request orchestration. The embedder service owns the Python/model runtime because n8n Code nodes are not designed to load Hugging Face models, manage model cache, or run local CPU inference.

### Local Reranker Service
**Role:** Stage 2 retrieval relevance runtime.  
**Responsibility:** Scores Stage 1 Qdrant candidates with `cross-encoder/ms-marco-MiniLM-L-6-v2` and returns `reranker_score` values.  
**Implementation:** Local FastAPI service called by n8n at `http://reranker:8002/rerank`.

n8n still owns the logical rerank node, threshold gate, refusal handling, context assembly, and observability. The reranker service exists only to host the Python/PyTorch CrossEncoder runtime. This avoids embedding heavyweight ML dependencies inside n8n and follows the same model-serving pattern as the embedder.

### Gemini API
**Role:** Cognitive engine.  
**Responsibility:** Evaluates relevance, structures data, and generates responses.  
**Implementation:** External API over HTTPS.

### Observability Layer
**Role:** Runtime tracking and evaluation layer.  
**Responsibility:** Captures telemetry, request/response payload traces, latency logs, evaluation data, and explicit user feedback.  
**Implementation:** Centralized logging/tracing service or database collection, such as a dedicated Qdrant/Postgres collection, Langfuse, or Arize.

## 2. End-to-End Data Flow and Observability Lifecycle
The live interaction loop operates through a shared intake and routing system. It processes explicit user requests, passive channel monitoring, and evaluation/regression invocations, transforming valid requests into context-aware answers or durable evaluation evidence while capturing full-lifecycle observability data at every stage.

```text
[ Discord Chat Traffic / Regression Runner / CI ] ---> (Every incoming request logged to Observability Layer)
       |
       v
[ n8n Intake + Routing Workflow ]
       |
       v
[ Mode Flags: trigger_source, run_mode, response_mode, permissions ]
       |
       v
[ Shared RAG Core: embed -> Qdrant -> rerank -> dedupe -> context ]
       |
       +--------------------------+
       |                          |
       v                          v
[ Optional Gemini Generation ]    [ Retrieval-only Result ]
       |                          |
       +------------+-------------+
                    |
                    v
       [ Mode-Specific Output Writers ]
       |        |            |
       v        v            v
[ Discord ] [ Regression ] [ CI Artifact / Metrics ]
```

The Discord output writer is an n8n HTTP Request node that calls the official
Discord Bot API. It dynamically uses the transaction's `channel_id`, so one bot
installation can respond in any permitted channel without per-channel webhook
configuration or another dispatch service.

### Incremental Ingestion Control Plane

n8n is the single coordinator for offline incremental ingestion. It invokes
deterministic Python planning/chunking helpers and the existing
embedder/Qdrant services, but owns scheduling, maintenance gates, execution
draining, retries, validation, rollback, and reopening.

Postgres is the durable source of truth for each run's current state and final
outcome, including phase results and timestamps, reconciled counts, failures,
regression, and rollback. Phoenix provides the correlated, detailed transition
timeline for debugging; it is not authoritative lifecycle storage.

Maintenance uses narrow transactional enter and exit operations rather than a
second general-purpose state machine. Entering maintenance atomically closes
the serving gate and records the run/runtime state. Draining then means waiting
only for online RAG executions that already passed that gate and may be using
Qdrant, the reranker, or Gemini. Durable Discord capture continues, while new
RAG reads are gated. The exit operation atomically reopens serving and records
the run outcome. A replacement plan must be fully `shadow_validated` before
maintenance can begin. Affected-point rollback snapshots are retained for
14 days.

### Execution Tracking Stages
1. **Universal Capture**  
   The moment a Discord event, regression case, or CI request reaches the n8n intake workflow, a unique transaction tracking ID is created. The raw request envelope and mode flags are logged to the Observability Layer, regardless of whether the request triggers a response.

2. **Dual-Ingress Routing and Filtering**  
   Active calls, such as direct bot mentions, bypass passive-listener relevance filters and move directly to the shared RAG core. Passive listener events are checked against operational heuristics, such as character length and question markers. Regression and CI requests set explicit evaluation mode flags and also use the shared RAG core. If a request is filtered out, the transaction status is updated to `Ignored` in the logs.

3. **Context Vector Retrieval Telemetry**  
   For valid queries, vector search execution metrics are appended to the transaction log. These metrics include query latency, confidence scores, and the exact chunks retrieved from Qdrant. If Qdrant does not return usable context, the failed context match is logged before the workflow either generates an active-call fallback or silently drops a passive-listener event.

4. **LLM Input/Output Guardrails**  
   The complete assembled prompt sent to the Gemini API and the returned response payload are captured alongside execution metadata, such as token counts and API latency.

5. **Dispatch Verification**  
   For Discord output, n8n posts through the authenticated Bot API to the
   originating channel and records the returned message ID. A final confirmation
   log marks whether the transaction was successfully transmitted to its
   configured output: Discord, regression result rows, CI artifact, or
   review/evaluation tables.

## 3. Feedback Loop and Performance Grading
To move beyond basic monitoring and toward continuous optimization, the architecture explicitly accounts for community evaluation.

### Feedback Sources
| Type     | Source                                                        | What It Tells Us                                      |
|----------|---------------------------------------------------------------|-------------------------------------------------------|
| Implicit | Reactions on bot messages, such as thumbs-up or thumbs-down.  | Whether the answer was broadly useful.                |
| Explicit | Context-menu command, slash command, or feedback form.        | What was wrong, missing, or confusing in the answer.  |

### Telemetry Mapping

Feedback events extract the target message ID, correlate it back to the original transaction tracking ID inside the Observability Layer, and append a performance grade such as `score: 1.0` or `score: 0.0`.

Over time, this creates a gold-standard dataset of system performance that can be used to evaluate retrieval quality, answer quality, and routing behavior.

## 4. Hosting and Infrastructure Blueprint

To minimize overhead, the backend infrastructure is self-contained within the existing infrastructure footprint.

| Area             | Design                                                                           | Notes                                                            |
|------------------|----------------------------------------------------------------------------------|------------------------------------------------------------------|
| Host             | OCI Always Free Tier on ARM64 Ampere compute.                                    | Up to 24 GB RAM available for the instance.                      |
| Containers       | Qdrant, Phoenix/Postgres, embedder, reranker, and trace emitter run beside n8n. | Keeps runtime dependencies local and self-contained. |
| Internal network | n8n reaches Qdrant, embedder, reranker, Phoenix, and Postgres over Docker service names. | Avoids container IP drift and keeps local calls off the public network. |
| External network | Discord, Gemini, and model downloads use outbound HTTPS. | Keeps third-party API traffic separate from local service traffic. |

## 5. Volunteer Implementation Scope

Volunteers own the implementation details that stem from this structural baseline.

| Workstream             | Responsibility                                                              | Output                                      |
|------------------------|-----------------------------------------------------------------------------|---------------------------------------------|
| Vector database schema | Design Qdrant collections, distance metrics, payload fields, and indexes.   | Searchable vector store with useful metadata. |
| Local model services   | Package embedder and reranker runtimes as lightweight internal HTTP services. | n8n can call CPU-bound models without carrying Python/PyTorch dependencies. |
| Bot engagement logic   | Configure n8n filtering for when the bot should respond or stay silent.     | Clear active/passive response rules.        |
| Observability backend  | Select and wire tracing storage, such as PostgreSQL, Langfuse, or Phoenix.  | Queryable logs for each bot transaction.    |
| Feedback correlation   | Define keys that link reactions and critiques back to prior inference logs. | Feedback data tied to the original bot answer. |
