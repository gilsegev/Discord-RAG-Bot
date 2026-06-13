# Architecture Overview: Community Knowledge RAG Bot

This document outlines the high-level architecture, component interactions, and hosting environment for the community Discord RAG Bot project. It serves as an end-to-end framework for engineering volunteers to guide concrete design, schema definition, and implementation.

## 1. System Components and Responsibility Matrix
The system relies on a lean, containerized stack designed to balance low operational overhead with flexible, event-driven orchestration and comprehensive runtime tracking.

### Discord Gateway
**Role:** Event source and user interface.  
**Responsibility:** Provides real-time messaging data from Discord.  
**Implementation:** Persistent WebSocket connection using `MESSAGE_CREATE` and reaction intents.

### n8n Orchestrator
**Role:** Event router, dual-ingress filtering engine for active and passive events, context assembly layer, LLM coordinator, and telemetry dispatcher.  
**Responsibility:** Handles event routing, filtering, context assembly, LLM coordination, and telemetry dispatch.  
**Implementation:** Advanced AI Workflow Canvas hosted on Oracle Cloud.

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
The live interaction loop operates through a dual-trigger ingress system. It processes both explicit user requests and passive channel monitoring, transforming chat traffic into context-aware answers while capturing full-lifecycle observability data at every stage.

```text
[ Discord Chat Traffic ] ---> (Every incoming event logged to Observability Layer)
       |
       v
[ n8n Ingress Router ]
       |
       +-----------------------------------------+
       |                                         |
       v                                         v
[ Event 1: Active Call ]                 [ Event 2: Passive Listener ]
(User tags @bot)                         (Standard channel traffic)
       |                                         |
       |                                         v
       |                                 [ Rule Engine / Relevance Check ]
       |                                         |
       |                              No match --+--> [ Trace: Ignored ] --> [ Drop ]
       |                                         |
       +-------------------------+---------------+
                                 |
                                 v
                       [ Embed Query ]
                                 |
                                 v
                       [ Qdrant Payload Query ]
                                 |
                                 v
                       [ CrossEncoder Rerank ]
                                 |
                                 v
              [ Log: Vector Latency & Context Matches ]
                                 |
                                 v
                       [ Gemini Generation ]
                                 |
                                 v
              [ Log: LLM Prompt, Token Count, Response Text ]
                                 |
                                 v
                       [ Fallback Evaluation ]
                                 |
             [ Did Qdrant find context? ]
                    |             |
                   Yes            No
                    |             |
                    v             v
      [ Discord Outbound Node ]   [ Log: Failed Context Match to Observability ]
                    |             |
                    |             v
                    |     [ Is Active Call? ]
                    |        |          |
                    |       Yes         No
                    |        |          |
                    |        v          v
                    | [ Generate: "I don't know" fallback ]    [ Drop / Silent End ]
                    |        |
                    +--------+
                             |
                             v
             [ Update Trace: Dispatched successfully ]
```

### Execution Tracking Stages
1. **Universal Capture**  
   The moment an event reaches the n8n workflow from the Discord Gateway, a unique transaction tracking ID is created. The raw payload is immediately logged to the Observability Layer, regardless of whether the event triggers a response.

2. **Dual-Ingress Routing and Filtering**  
   Active calls, such as direct bot mentions, bypass relevance filters and move directly to vector retrieval with a strict "must answer" constraint. Passive listener events are checked against operational heuristics, such as character length and question markers. If filtered out, the transaction status is updated to `Ignored` in the logs.

3. **Context Vector Retrieval Telemetry**  
   For valid queries, vector search execution metrics are appended to the transaction log. These metrics include query latency, confidence scores, and the exact chunks retrieved from Qdrant. If Qdrant does not return usable context, the failed context match is logged before the workflow either generates an active-call fallback or silently drops a passive-listener event.

4. **LLM Input/Output Guardrails**  
   The complete assembled prompt sent to the Gemini API and the returned response payload are captured alongside execution metadata, such as token counts and API latency.

5. **Dispatch Verification**  
   A final confirmation log marks whether the transaction was successfully transmitted back to the Discord client.

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
