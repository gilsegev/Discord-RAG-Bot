# Phase 9: Passive Listener

## Status

The Phase 9 intake workflow and dedicated Discord Gateway listener are deployed.
Durable capture and the Phase 9C.4 incremental production replacement path are
now proven in production (August 12, 2026). This document remains the passive
listener design reference; `docs/n8n execution plan.md` is authoritative for
the current Phase 9C execution status and the planned 9C.5/9C.6 work.

## Goal

Phase 9 replaces the Phase 8 intake with smarter routing for active, passive, and
ignored Discord events. Active calls continue directly into the shared RAG core.
Ordinary messages first pass a lightweight relevance gate so obvious noise does
not consume embedding, Qdrant, reranker, or Gemini work.

The first implementation is a **shadow-mode passive listener**:

```text
Discord message
-> classify active / passive / ignored
-> passive relevance gate when needed
-> Shared RAG Core for active calls and relevant passive candidates
-> retrieval, reranking, dedupe, and context assembly
-> optional Gemini generation
-> Postgres and Phoenix evidence
-> no Discord post
```

This lets the team measure how often passive listening would find useful context,
what it would answer, and how noisy it would be before enabling any user-visible
behavior.

## Interpretation Of The Existing Design

The earlier design documents describe passive listening as a path that:

- receives ordinary channel messages
- classifies them as passive candidates or ignored events
- applies passive relevance and retrieval gates
- silently drops weak candidates
- may eventually post a response for a strong candidate

Phase 9 should not begin with that final user-visible step. The recommended first
stage is to run eligible messages in shadow mode and force:

```json
{
  "trigger_source": "discord_passive",
  "run_mode": "full_answer",
  "response_mode": "postgres_only",
  "allow_gemini": true,
  "allow_discord_post": false
}
```

Eligible Phase 9 passive candidates always run in `full_answer` mode with Gemini
enabled so reviewers can assess the complete hypothetical response. Retrieval-only
execution remains available to regression and evaluator traffic, not passive
shadow traffic. `allow_discord_post` must remain `false` for all Phase 9
shadow-mode events, regardless of caller input.

## Scope

### In Scope

- accept ordinary Discord message events through the shared intake contract
- identify the event as `discord_passive`
- create one durable transaction per eligible message
- use the existing Phase 8 shared RAG core
- record retrieval, reranking, dedupe, context, refusal, and optional generation
  results
- log why an event was excluded or stopped
- guarantee that passive shadow-mode events cannot post to Discord
- provide enough evidence to decide whether a later visible passive mode is
  desirable

### Out Of Scope

- posting passive answers to Discord
- changing active-call behavior
- duplicating the RAG core
- feedback correlation
- weekly metrics and alerts
- automatic tuning of thresholds from unreviewed shadow-mode results
- treating an ignored passive event as a user-facing refusal

## Event Eligibility

At the current server volume, the gate should be conservative rather than
aggressive: it should remove obvious non-questions and noise while allowing
uncertain but potentially useful messages into shadow-mode retrieval.

Exclude before retrieval:

- messages authored by this bot or another configured bot
- webhook, integration, system, join, boost, or other non-user message events
- message edits and duplicate delivery of an already-seen message ID
- empty messages and attachment-only messages with no supported text
- messages from explicitly excluded channels
- active bot mentions, which must continue through the active-call route
- acknowledgement-only messages such as "thanks", "got it", or "sounds good"
- emoji-only, URL-only, or punctuation-only messages
- very short conversational fragments with no question or help-seeking signal

Do not initially exclude messages merely because they:

- lack a question mark but contain a clear help-seeking phrase
- are short but contain a clear question
- look conversational rather than like a direct question
- are unlikely to produce an answer

The gate must store a stable decision reason for every ignored message. Its rules
should be configurable and covered by focused routing tests. Shadow outcomes can
then show whether the gate is too broad or too strict.

## Routing Contract

The intake workflow remains the owner of routing and side-effect policy.

Phase 9 versions the Phase 8 intake workflow rather than wrapping it unchanged.
This is an intentional improvement to the production intake contract. The Phase
8 workflow remains a historical artifact, while regression and active-call
traffic move to the Phase 9 intake.

| Input | Passive shadow-mode value |
|---|---|
| `trigger_source` | `discord_passive` |
| `route_type` | `passive_candidate` |
| `run_mode` | Always forced to `full_answer` |
| `response_mode` | `postgres_only` |
| `allow_gemini` | Always forced to `true` |
| `allow_discord_post` | Always forced to `false` |
| `requested_by` | `bot` |

An ineligible event should be persisted as `route_type = ignored`,
`status = ignored`, `retrieval_status = not_started`, and
`response_status = not_posted`. Its decision reason belongs in
`rag_trace_events.event_payload`.

An eligible passive event runs through the shared core. A retrieval or context
refusal is a silent analytical outcome: it is persisted but never dispatched.

Routing order:

```text
regression / evaluator request -> active_call -> shared core
direct bot mention            -> active_call -> shared core
ordinary Discord message      -> passive relevance gate
                                 -> relevant: passive_candidate -> shared core
                                 -> irrelevant: ignored -> stop
bot/system/duplicate event     -> ignored -> stop
```

## Retrieval And Generation Policy

Shadow mode should initially use the same RAG core and thresholds as active calls.
This creates a comparable baseline and avoids guessing at stricter passive
thresholds before evidence exists.

The request contract must continue to carry configurable thresholds so shadow
data can later be replayed or compared against candidate policies. A higher
passive threshold should be adopted only after reviewing:

- how often active thresholds find context for ordinary conversation
- how many results a reviewer considers relevant
- how many generated answers would have been intrusive or off-topic
- false negatives created by a stricter threshold

Every eligible passive candidate uses `full_answer`: generate the hypothetical
answer, store it for review, and do not post it. At 30–50 messages per day,
processing all eligible messages is a reasonable initial calibration workload.
Regression and evaluator requests retain their independent retrieval-only mode
and must not inherit the passive Gemini policy.

## Rate Limiting Recommendation

Traffic of approximately 30–50 messages per day does not justify a restrictive
product rate limit. A low limit would discard the very evidence shadow mode is
intended to collect.

Phase 9 should distinguish three concerns:

### 1. Normal traffic

Do not rate-limit normal current traffic. Process every eligible message.

### 2. Duplicate and burst protection

Always deduplicate by Discord message ID. Optionally place a generous emergency
ceiling on passive work, disabled by default or set far above normal traffic.
Example guardrails are:

- a configurable maximum number of passive starts per minute
- a configurable maximum number of concurrent passive executions
- a global passive-listener kill switch

These are operational circuit breakers for a Discord reconnect, replay, spam
burst, or event-loop bug. They are not relevance rules and should not shape
normal use.

### 3. Resource pressure

If the server becomes constrained, reduce passive work before active-call work.
Prefer bounded concurrency or queueing over silently discarding messages. If an
event must be dropped, persist `routing.ignored` with a reason such as
`passive_emergency_limit` so the loss is measurable.

Recommended initial settings:

```json
{
  "passive_enabled": true,
  "passive_allow_discord_post": false,
  "passive_rate_limit_enabled": false,
  "passive_max_concurrency": 1
}
```

The concurrency value is a starting operational safeguard, not a throughput
requirement. It should be validated against actual n8n execution time and may be
raised if messages queue unnecessarily.

## Observability

Each Discord message received by the passive listener must produce either a
durable ignored decision or a traceable shared-core execution.

Use the existing event taxonomy:

- `discord.event_received`
- `routing.active_call`
- `routing.passive_candidate`
- `routing.ignored`
- existing retrieval, rerank, dedupe, context, and Gemini events

Passive decision attributes should include:

- `transaction_id`
- `discord_message_id`
- `channel_id`
- `trigger_source`
- `route_type`
- `passive_mode = shadow`
- `eligibility_status`
- `eligibility_reason`
- `allow_gemini`
- `allow_discord_post`
- thresholds used
- final retrieval/context outcome
- whether a hypothetical answer was generated

Full message text should follow the existing privacy and retention policy.
Hashes and short summaries should be preferred in traces when full text is not
needed.

## Safety Invariant

The workflow must enforce this invariant at more than one boundary:

```text
trigger_source = discord_passive
AND passive_mode = shadow
=> allow_discord_post = false
```

The intake workflow should force the flag off, and the final dispatch decision
should independently reject passive shadow-mode posting. A caller-provided
`allow_discord_post = true` must not override either check.

## Validation Cases

The first implementation should demonstrate:

- a normal human message enters the shared RAG core
- a passive event can generate and persist a hypothetical answer
- a retrieval-only regression does not execute Gemini
- no passive event posts an answer or refusal to Discord
- an active mention still follows the active-call path and can post normally
- bot-authored, system, empty, excluded-channel, duplicate, and active-mention
  events are routed correctly
- weak retrieval is stored as a silent outcome
- operational failures remain distinguishable from weak/no-context outcomes
- the passive kill switch prevents new passive core executions
- duplicate Discord delivery does not create duplicate pipeline work

## Success Criteria

- ordinary eligible Discord messages use the Phase 8 shared RAG core
- shadow-mode posting is impossible even with conflicting input flags
- current server traffic can be processed without an arbitrary product rate
  limit
- ignored and stopped events have explicit durable reasons
- active-call and regression behavior remains unchanged
- the team can review passive retrieval and hypothetical-answer quality before
  deciding whether to build a visible passive responder

## Future Promotion To Visible Passive Responses

Posting passive answers is a separate Phase 9B product decision, not an automatic
Phase 9 outcome. Phase 9B should start only after the active-call bot has
completed a production pilot and the team has reviewed Phase 9 shadow-mode
evidence. Promotion requires an explicit change to the contract.

Before enabling it, define:

- which channels opt in
- what message intent is eligible
- stricter retrieval and reranker thresholds, if evidence supports them
- cooldown and conversation-level anti-noise behavior
- how users can suppress or opt out of passive responses
- an initial canary and rollback plan

## Open Decisions

- Should the initial run use `retrieval_only` for all messages, `full_answer` for
  all messages, or retrieval for all with sampled Gemini generation?
- Which channels are included or excluded from shadow observation?
- Should attachment text, links, and reply context be added to the query, or
  should v1 use message content only?
- Where will generated shadow answers be retained for review?
- Is a passive concurrency cap needed after measuring real execution latency?
