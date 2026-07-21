# Passive Query Answerability Design Proposal

**Status:** Proposed

**Reviewer:** Haragonda

**Scope:** Phase 9 passive Discord intake and shared RAG response gating

**Motivating transaction:** `580a47bb-738e-43c7-9622-8c7532acea1d`

## 1. Executive Summary

The passive listener currently treats almost any question-shaped message as a RAG candidate. This allows referentially incomplete messages such as `does it really matter?` to reach retrieval and generation even when the message does not identify what `it` means.

In the motivating transaction, retrieval found historical messages containing nearly the same phrase but concerning two unrelated subjects: behavioral interview examples and PMP certification. Gemini then produced a fluent, cited answer covering both subjects. The citations were real, but the response was not grounded in the user's intended meaning because no intended meaning could be determined from the standalone message.

The transaction ran in shadow mode with `allow_discord_post=false`, so no Discord response was posted. The result nevertheless demonstrates a launch-blocking false-positive pattern for passive posting.

This proposal adds an answerability gate before embedding and retrieval. Passive messages with unresolved references or no identifiable topic should be silently ignored unless safe conversational context supplies the missing subject. Direct bot invocations may ask for clarification instead of remaining silent.

## 2. Incident Evidence

### 2.1 Intake

The transaction contained:

| Field | Value |
|---|---|
| Query | `does it really matter?` |
| Channel | `#offtopic` |
| Direct bot mention | `false` |
| Route | `passive_candidate` |
| Routing reason | `passive_relevance_gate_passed` |
| Response mode | `postgres_only` |
| Discord posting allowed | `false` |

The current passive intake recognized a question signal because the message contained both `does` and a question mark. The minimum-word safeguard did not reject it because question signals bypass that safeguard.

### 2.2 Retrieval and generation

| Stage | Result |
|---|---|
| Raw Qdrant candidates | 20 |
| Candidates passing retrieval threshold `0.55` | 20 |
| Candidates passing reranker threshold `> 0` | 2 |
| Selected context count | 2 |
| Final status | `answered` |
| Model refusal | `false` |
| Citation guard failure | `false` |

The selected context was:

1. A 2021 discussion asking whether behavioral interview examples need large-scale rather than small-scale impact. Retrieval score: `0.6783`; reranker score: `4.7606`.
2. A 2022 discussion asking whether PMP certification matters to employers. Retrieval score: `0.6330`; reranker score: `1.4313`.

The first result scored strongly because it contained the near-exact wording `Does it really matters`. The model interpreted the retrieved subjects as possible meanings of the user's unresolved `it` and answered both.

## 3. Problem Statement

The current pipeline measures whether corpus text is similar to the submitted words. It does not first establish whether the submitted words form an independently answerable request.

This creates a distinction the current gates do not represent:

- **Source grounding:** The response can be traced to real retrieved text.
- **Intent grounding:** The retrieved text addresses the subject the user meant.

The motivating response passed source-grounding checks but could not pass intent grounding because the standalone query never supplied a subject. Valid citations therefore made the response look more trustworthy without making it relevant.

This issue cannot be reliably fixed by increasing retrieval or reranker thresholds. A phrase-level match can score highly precisely because the query is generic. The top result in this transaction had a reranker score of `4.7606` and was still unsafe to answer.

## 4. Design Goals

1. Do not answer passive messages whose meaning depends on an unresolved referent.
2. Preserve natural multi-message conversation when the missing subject can be attributed safely.
3. Avoid attaching unrelated nearby channel messages as context.
4. Keep active calls usable by asking for clarification when appropriate.
5. Make every ignore, clarification, and answerability decision observable and regression-testable.
6. Keep the answerability decision separate from retrieval relevance and generation groundedness.

## 5. Non-Goals

- Replacing Qdrant, the embedder, or the CrossEncoder reranker.
- Solving general conversation understanding across an unlimited channel history.
- Allowing the model to infer a subject from arbitrary nearby messages.
- Using higher retrieval scores as a substitute for query completeness.
- Enabling passive Discord posting as part of this change.

## 6. Proposed Design

### 6.1 Add a pre-retrieval answerability gate

After intake normalization and before the shared RAG core is called, classify the request into one of these states:

| State | Meaning | Passive behavior | Direct invocation behavior |
|---|---|---|---|
| `answerable` | A concrete subject or request is present. | Continue to RAG. | Continue to RAG. |
| `answerable_with_context` | Approved reply or same-author context supplies the subject. | Continue using an explicitly assembled query. | Continue using an explicitly assembled query. |
| `ambiguous_missing_referent` | Terms such as `it`, `this`, or `that` have no attributable referent. | Silently ignore. | Ask for clarification. |
| `not_a_request` | Acknowledgement, reaction, conversational fragment, or rhetorical fragment. | Silently ignore. | Do not invoke RAG; optionally acknowledge only if product policy requires it. |
| `uncertain` | The gate cannot determine answerability confidently. | Silently ignore. | Ask for clarification. |

The gate should fail closed for passive messages.

### 6.2 Detect referential incompleteness

The deterministic first pass should identify common unresolved forms, including:

- Pronouns and demonstratives: `it`, `this`, `that`, `they`, `them`, `those`, `there`.
- Elliptical questions: `does it matter?`, `is that worth it?`, `would that help?`, `what about this?`, `why not?`.
- Topic-free evaluative language: `is it good?`, `is that normal?`, `does anyone care?`.

Detection should not reject a message merely because it contains a pronoun. It should reject when the message contains no concrete noun phrase, named entity, quoted text, link context, slash-command argument, or approved conversational context that resolves the reference.

A small model-based classifier may follow the deterministic pass for borderline cases, but its output must be a constrained decision object rather than a generated answer. Passive mode should require a high-confidence `answerable` result.

### 6.3 Resolve conversational context conservatively

Context may be used only through explicit attribution rules:

1. **Discord reply context:** If the message replies to another message, use the replied-to content as the primary referent source.
2. **Same-author message burst:** Buffer adjacent messages from the same author for a short configurable debounce window and classify the combined text.
3. **Thread ownership:** A thread starter may be included when the message is inside that thread and the relationship is explicit.
4. **No arbitrary channel lookback:** Do not use the most recent message from another author merely because it is nearby in time.

The assembled text used for classification and retrieval should be stored separately from the original message:

```json
{
  "original_user_query": "does it matter?",
  "resolved_query": "Does PMP certification matter for Big Tech TPM roles?",
  "context_resolution_source": "discord_reply",
  "context_message_ids": ["..."],
  "answerability_status": "answerable_with_context"
}
```

The system must not silently rewrite an ambiguous query unless it can record the specific message relationship that justified the rewrite.

### 6.4 Routing behavior

For the motivating transaction, the expected routing result is:

```json
{
  "route_type": "ignored",
  "routing_reason": "ambiguous_missing_referent",
  "answerability_status": "ambiguous_missing_referent",
  "should_run_rag": false,
  "allow_gemini": false,
  "allow_discord_post": false
}
```

For a direct mention such as `@bot does it matter?`, the bot should not retrieve arbitrary corpus meanings. It should return a brief clarification request such as:

> What are you referring to?

Clarification responses should be generated from fixed templates and should not require RAG or Gemini.

### 6.5 Add a post-retrieval defense in depth

The pre-retrieval gate is the primary fix. The RAG core should also reject generation when either condition is true:

- `answerability_status` is missing or is not an approved answerable state for a passive request.
- Context candidates disagree on the apparent subject of an underspecified query.

The current weak-reranker calculation should not be treated as an answerability check. It only fires when every selected result is below the weak threshold, so one high lexical match can mask unrelated or weak candidates.

A later context-sufficiency evaluator may compare the explicit resolved subject against each selected chunk. That evaluator should supplement, not replace, the pre-retrieval gate.

## 7. Observability Contract

Add these transaction and trace fields:

| Field | Example |
|---|---|
| `answerability_status` | `ambiguous_missing_referent` |
| `answerability_reason` | `unresolved_pronoun_it` |
| `answerability_confidence` | `1.0` for deterministic rules |
| `original_user_query` | `does it really matter?` |
| `resolved_query` | Empty when unresolved |
| `context_resolution_source` | `none`, `discord_reply`, `same_author_burst`, or `thread_starter` |
| `context_message_ids` | IDs used to resolve the query |
| `rag_skipped` | `true` |
| `rag_skip_reason` | `query_not_answerable` |

Emit a trace event such as `intake.answerability_checked` before embedding. Dashboard reporting should distinguish:

- Ignored ambiguous passive messages.
- Direct invocations that received clarification.
- Context-resolved messages that continued to RAG.
- Answerability-gate overrides or unexpected missing classifications.

## 8. Regression Plan

Add regression coverage for at least these cases:

| Case | Context | Expected result |
|---|---|---|
| `Does it matter?` | Standalone passive message | Ignore: `ambiguous_missing_referent` |
| `Is that worth it?` | Standalone passive message | Ignore |
| `Would that help?` | Standalone passive message | Ignore |
| `What about this?` | Standalone passive message | Ignore |
| `Does PMP certification matter for Big Tech TPM roles?` | Standalone passive message | Eligible for RAG |
| `Does it matter?` | Reply to a PMP question | Resolve from reply; eligible for RAG |
| `Does it matter?` | Nearby unrelated message from another author | Ignore; do not borrow context |
| `I am considering PMP certification` followed by `Does it matter for Big Tech?` | Same-author message burst | Combine and classify as answerable |
| `@bot does it matter?` | Direct invocation | Ask for clarification without RAG |
| `It depends on the company and role.` | Passive comment | Ignore as `not_a_request` |

Acceptance criteria before passive posting is enabled:

1. All standalone ambiguous cases are silently ignored.
2. No test resolves context from an unrelated author solely by proximity.
3. Reply-linked and same-author multi-message cases retain the correct subject.
4. Ignored cases do not call embedding, Qdrant, the reranker, or Gemini.
5. Every decision is visible in the transaction record and Phoenix trace.
6. Existing explicit, answerable regression questions do not suffer a material increase in false refusals.

## 9. Rollout Plan

1. Implement the answerability fields and decision logic while passive mode remains shadow-only.
2. Replay historical passive transactions and measure the distribution of decision states.
3. Manually review samples from `answerable_with_context` and `uncertain` because these carry the highest attribution risk.
4. Add regression cases and establish false-positive and false-refusal thresholds.
5. Enable clarification only for direct invocations.
6. Consider passive posting only after the acceptance criteria hold over an agreed observation window.

## 10. Risks and Tradeoffs

### False ignores

Failing closed will ignore some short questions that a person could understand from informal channel context. This is preferable to an unsolicited, authoritative-looking answer about the wrong subject. Reply metadata and same-author buffering recover the safest subset.

### Added latency

A debounce window for multi-message bursts delays passive evaluation. This is acceptable because passive answers are unsolicited and correctness is more important than immediate response time.

### Context contamination

Broad channel-history lookup could reduce false ignores but creates a larger risk of joining unrelated conversations. Context resolution should therefore be relationship-based, not merely time-based.

### Rule maintenance

Deterministic referent patterns will not cover every ambiguous phrase. The system should log `uncertain` examples for review and evolve the regression set rather than defaulting uncertain passive messages to RAG.

## 11. Decision Requested

Haragonda should decide:

1. Whether passive mode should fail closed on `ambiguous_missing_referent` and `uncertain`.
2. Which context-resolution sources are approved for the first implementation: Discord replies only, or replies plus same-author message bursts.
3. The debounce duration and maximum number of messages for same-author bursts.
4. Whether a lightweight model classifier is acceptable after deterministic checks, or whether the first version should remain rules-only.
5. The shadow observation period and quality thresholds required before passive Discord posting can be considered.

## 12. Recommended Decision

Adopt the pre-retrieval answerability gate and fail closed for passive traffic. Start with deterministic ambiguity detection plus Discord reply context. Add same-author buffering only after message attribution and timing behavior are observable and regression-tested. Keep passive posting disabled until the new gate has been evaluated in shadow mode.
