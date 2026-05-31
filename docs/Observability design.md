# Observability and Evaluation Design

**Status:** Draft for review  
**Related docs:** [Architecture Overview](Arch%20overview.md), Evaluation and Feedback Scoring Design  
**Purpose:** Define the observability layer for the community knowledge RAG bot.

This document describes how we log pipeline execution, measure response quality, process community feedback, and automate regression testing using a unified telemetry backend.

## 1. Core Observability Stack

We are adopting **Arize Phoenix** as the centralized observability and evaluation platform.

| Decision | Rationale |
| --- | --- |
| Use Phoenix as the primary observability backend | Phoenix can run as a single Docker container, which fits the Oracle Cloud 1 CPU resource constraint. |
| Use OpenTelemetry as the trace protocol | n8n can send structured execution traces over standard HTTP without requiring custom integration code. |
| Keep logs, traces, feedback, and evaluations in one platform | This avoids operating separate services for logging, evaluation, and experiment tracking. |

### Why Phoenix

Phoenix gives us one place to inspect:

- Raw user inputs
- Retrieved Qdrant chunks
- Final prompts sent to the model
- Generated responses
- Latency and failure modes
- User feedback
- Evaluation results
- Regression test outcomes

This keeps the first production version operationally small while still giving contributors enough visibility to debug RAG quality.

## 2. Telemetry Ingress Interfaces

Every bot interaction should generate a trace that Phoenix can capture. The n8n orchestrator owns routing and trace submission.

| Trace Type | Trigger | What Gets Logged | Primary Key |
| --- | --- | --- | --- |
| Execution trace | Direct bot ping or passive listener match | Raw user input, routing path, retrieved Qdrant chunks, Gemini prompt, generated output, latency, and final status. | Transaction ID |
| Feedback trace | User reacts to a bot message with thumbs-up or thumbs-down | Discord message ID, reaction type, user feedback metadata, and linked execution trace. | Discord bot message ID |

### Execution Traces

When the n8n pipeline fires, it should create a unique transaction ID and send the following to Phoenix:

- Trigger type: active call or passive listener
- Raw Discord event payload
- User-visible question or message text
- Filtering decision and reason
- Qdrant query text
- Retrieved chunk IDs, scores, and payload snippets
- Prompt sent to Gemini
- Gemini response
- Token counts
- Latency by stage
- Final dispatch status

### Feedback Traces

When a user reacts to a bot message, a lightweight n8n webhook should:

1. Capture the Discord reaction event.
2. Extract the Discord bot message ID.
3. Query Phoenix for the original execution trace.
4. Append the feedback signal to that trace.
5. Mark the trace for review if the feedback is negative.

## 3. Evaluation Metrics

We measure quality across two primary dimensions:

- **System reliability:** Did the RAG pipeline retrieve and use context correctly?
- **Community usefulness:** Did the answer help the user in the way the community expects?

We will track a normalized index over time rather than relying on absolute traffic volume.

## 4. RAG Reliability Index

The **RAG Reliability Index (RRI)** is the core trajectory metric.

RRI is a weighted percentage calculated from human-verified audits of production logs. To avoid annotator fatigue, production traces are graded with strict binary outcomes rather than a granular numerical scale.

| Metric | Grade | Pass Criteria | Fail Criteria |
| --- | --- | --- | --- |
| Groundedness | Pass / Fail | The response relies only on provided Qdrant context. | Any fabricated claim, unsupported inference, or external assumption. |
| Appropriate refusal | Pass / Fail | If no context is found, the bot uses the required refusal behavior. | The bot answers without evidence or fails to use the expected refusal. |
| Framing and tone | Pass / Fail | The bot includes appropriate community nuance and avoids overclaiming. | The bot sounds authoritative on subjective, personal, or time-sensitive topics. |

### Suggested RRI Formula

The exact weights can be tuned, but a starting point is:

```text
RRI = (0.50 * Groundedness pass rate)
    + (0.30 * Appropriate refusal pass rate)
    + (0.20 * Framing and tone pass rate)
```

Groundedness carries the highest weight because unsupported answers are the most serious RAG failure mode.

## 5. CI/CD Pipeline and Regression Datasets

We will automate baseline testing so changes to vector search parameters, chunking, prompts, or LLM settings do not quietly degrade performance.

### Baseline Dataset

We will curate a fixed dataset of **40 to 60 historical community questions**.

The dataset should include:

- Happy-path questions with clear answers in the indexed history
- Nuanced questions that require careful framing
- PII and safety-sensitive checks
- Strict no-context cases that should force refusal
- Retrieval edge cases where similar-but-wrong chunks are easy to retrieve

### Storage

The baseline dataset should live in Phoenix under the **Experiments** module. We will not use spreadsheets as the source of truth.

### CI Trigger

On code changes to the main branch, a script should trigger the Phoenix API.

Phoenix should then:

1. Run the baseline dataset through the n8n pipeline.
2. Capture outputs and trace metadata.
3. Grade outputs against the configured evaluation criteria.
4. Compare results against previous runs.
5. Block merge or flag review if groundedness drops below the agreed threshold.

## 6. Feedback and Triage Loop

User feedback from Discord is triaged, not automatically trusted.

### Triage Queue

A thumbs-down reaction does not always mean the system failed. It may mean:

- The user disagrees with the community consensus
- The answer was accurate but incomplete
- The answer was poorly framed
- Retrieval found weak context
- The model hallucinated or overgeneralized

All negative feedback events should be flagged in Phoenix for human review.

### Dataset Promotion

An annotator reviews each flagged trace.

If the thumbs-down was caused by a real system failure, the trace should be cleaned and promoted into the CI regression dataset.

Examples of promotable failures:

- Hallucination
- Poor vector retrieval
- Missing refusal
- Wrong source context
- Overconfident answer
- Bad tone for a nuanced topic

Once promoted, that failure mode should become part of the permanent regression suite so it is less likely to recur.

## 7. Open Questions

| Question | Why It Matters |
| --- | --- |
| What exact refusal text should active-call no-context cases use? | The evaluator needs a stable expected behavior. |
| What groundedness threshold should block merge? | Too strict may slow iteration; too loose may allow quality regressions. |
| Who owns human trace review? | Negative feedback only improves the system if someone triages it. |
| How long should raw Discord payloads be retained? | We need to balance debugging value with privacy and data minimization. |
| Should Phoenix be public to contributors or restricted to maintainers? | Trace data may include sensitive community content. |
