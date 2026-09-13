# Passive Intent Gate Design

**Status:** Implemented in shadow mode
**Owner:** Discord RAG Bot maintainers
**Related:** Phase 9; Retrieval, Context & Prompt Contracts; Observability Design; Regression README

## 1. Purpose

Passive routing currently uses broad lexical signals before sending ordinary Discord messages through embedding, retrieval, reranking, and generation. A fourteen-day production shadow review ending 2026-09-13 found that 49 of 179 passive candidates were marked answered. Thirty-seven of those answers had no question mark, and manual review found that most answered messages were replies, acknowledgements, coordination, job or referral activity, advertisements, or statements rather than requests for historical community knowledge.

This design adds a conservative intent contract before the shared RAG core. It protects the shared retrieval path from non-questions without duplicating that path or changing explicit bot invocation behavior.

## 2. Decision

Use three ordered gate levels for passive Discord messages:

```text
Discord intake
    |
    +-- explicit bot invocation --------------------------> shared RAG core
    |
    +-- passive message
            |
            v
      Level 1: deterministic exclusion
            | excluded
            +---------------------------------------------> ignored
            |
            v
      Level 2: knowledge-request admission
            | admitted
            +---------------------------------------------> shared RAG core
            |
            v
      Level 3: ambiguous disposition ---------------------> ignored
```

Passive admission is intentionally precision-first. A member can always mention the bot explicitly when a message is ambiguous or when they want an answer despite the passive policy.

## 3. Contract

The gate consumes the normalized Discord intake fields already available before retrieval. It does not inspect retrieved content and does not call Gemini.

The gate produces:

| Field | Meaning |
|---|---|
| `route_type` | Existing contract: `active_call`, `passive_candidate`, or `ignored`. |
| `routing_reason` | Stable machine-readable reason for the final decision. |
| `passive_gate_level` | `bypass`, `level_1`, `level_2`, or `level_3`. |
| `passive_gate_policy_version` | Version of the deterministic intent policy. |
| `passive_intent_class` | `knowledge_request`, `conversation`, `coordination`, `job_or_referral`, `promotion`, or `ambiguous`. |
| `passive_intent_confidence` | `high`, `medium`, or `low`; this is policy confidence, not model probability. |
| `passive_intent_evidence` | Bounded list of stable signal names used by the decision. |

These fields travel with the existing workflow state and are written into routing trace events. The transaction schema and existing route-type values remain unchanged.

## 4. Gate Levels

### 4.1 Level 1: deterministic exclusion

Level 1 rejects messages whose dominant intent is identifiable with high precision before evaluating whether their topic resembles the corpus.

Initial exclusions:

- Platform-invalid events already covered by Phase 9: duplicates, bot or webhook authors, system events, excluded channels, empty content, and URL-only content.
- Acknowledgements and reactions such as thanks, agreement, confirmation, or conversational closure.
- Coordination such as meeting times, attendance, availability, sign-up logistics, or link confirmation.
- Job and referral transactions such as openings, recruiter introductions, requests to refer, requests to connect, or direct-message calls to action.
- Promotions, surveys, solicitations, and advertisements.
- Clear conversational answers, advice, anecdotes, or status updates that do not request information.

Exclusions must use bounded phrase and clause patterns. A keyword appearing anywhere in a message is not sufficient. Channel is supporting evidence, not an automatic exclusion, because knowledge questions can appear in job, referral, or event channels.

### 4.2 Level 2: knowledge-request admission

Level 2 admits only messages with both a strong request for information and
corpus-oriented evidence that historical TPM Unite conversations could answer
it. Grammatical question syntax alone is not enough in passive mode: it also
appears in clarifications, live coordination, current-status checks, rhetorical
remarks, and replies directed at another member.

Admission requires both of the following:

1. At least one clause-level request signal:
   - Direct interrogative construction such as `how`, `what`, `why`, `when`, `where`, `who`, or `which` introducing a question clause.
   - Auxiliary-led question construction such as `can anyone`, `has anyone`, `should I`, `is it`, or `do people`.
   - Explicit information request such as `looking for advice`, `seeking recommendations`, `would appreciate insights`, or `curious about others' experience`.
2. At least one corpus-oriented signal: a stable, historically answerable TPM
   topic (for example an interview process, leveling, role scope, career
   practice, contract hiring, or company experience), or explicit TPM Unite
   historical-community framing such as asking about member or community
   experience, advice, or recommendations. Request words alone do not qualify:
   the message must establish professional/TPM domain evidence or expressly
   seek TPM Unite's historical community knowledge. General advice requests
   about choosing a car, wedding planning, or renewing an H-1B visa are Level 3
   outcomes unless they also establish that supported corpus relationship.

An actual request marker must be present. For example, `any insights on
contract hiring patterns` is an explicit request; a declarative statement that
someone “has insight” is not. Topic nouns such as `insight`, `advice`,
`interview`, or a company name do not independently establish request intent.

Admission is blocked when a stronger Level 1 intent applies. A question mark
alone is insufficient because rhetorical questions and coordination questions
are not corpus-answerable knowledge requests. A request signal without
corpus-oriented evidence is a Level 3 ignored outcome. Conversely, a question
mark is not required when both an explicit information-request construction and
corpus-oriented evidence are present.

The existing broad heuristic that treats words such as `is`, `are`, `help`, `experience`, or `anyone` anywhere in a message as a question signal is retired.

### 4.3 Level 3: ambiguous disposition

Every passive message not excluded by Level 1 or admitted by Level 2 is ambiguous. Ambiguous passive messages are ignored without retrieval or generation.

Level 3 does not introduce an LLM classifier in this implementation. The production evidence-first dataset is small, the current failure is excessive admission, and an LLM pre-classifier would add cost and another nondeterministic contract before the evidence supports it. A learned or model-based classifier may be proposed later using reviewed labels, but it must preserve fail-closed behavior and demonstrate a material recall improvement without reducing admission precision.

## 5. Active Calls And Posting

- Explicit bot mentions and supported active/regression triggers bypass all passive gates.
- Duplicate protection remains ahead of the bypass and continues to ignore duplicate events.
- Passive candidates remain `full_answer` plus `postgres_only` shadow evaluations.
- Passive Discord posting remains disabled. Enabling it requires a separate reviewed decision after the acceptance metrics are met.

## 6. Observability

Each routing trace records the gate level, policy version, intent class, confidence, and evidence in addition to the existing route and reason.

Operational review should report:

- Passive messages evaluated, admitted, and ignored.
- Decisions by reason, intent class, channel, and policy version.
- Ambiguous rate.
- Human-reviewed precision among admitted passive requests.
- Recall against the labeled knowledge-request fixture set.
- Downstream context-found, refusal, and inappropriate-answer rates.
- Retrieval, generation-token, and latency cost avoided by pre-RAG rejection.

No raw message content is added to trace payloads by this design.

## 7. Verification Dataset

The reviewed production sample becomes a sanitized, fixed routing fixture set rather than part of the answer-quality regression suite. It must include known false positives, genuine historical-knowledge questions with and without question marks, ambiguous statements, and explicit bot mentions using otherwise excluded text.

Fixtures store synthetic or sanitized text and expected gate outcomes; they do not retain member identifiers or private contact information.

## 8. Rollout

1. Replay the fixed routing fixtures locally against the workflow code node.
2. Push the intake workflow with the repository workflow-sync script.
3. Keep passive behavior in `postgres_only` shadow mode.
4. Review gate metrics and a human-labeled sample after at least one representative week. In particular, compare question-syntax-only traffic with admitted traffic; the two-week replay established that grammatical form alone is not a safe proxy for corpus-answerable intent.
5. Tune by adding evidence-backed fixtures, not by broadening global keyword lists or relaxing the corpus-oriented evidence requirement.
6. Consider visible passive answers only through a separate approval after the acceptance criteria pass.

## 9. Acceptance Criteria

- At least 95% of admitted passive messages are genuine, corpus-answerable knowledge requests in human review.
- At least 90% of labeled legitimate knowledge requests are admitted.
- No known production-derived non-question fixture reaches the shared RAG core.
- Explicit bot mentions and regression calls preserve their existing behavior.
- Every passive decision has a stable reason and complete gate telemetry.
- Existing retrieval and full-answer regressions remain unchanged.
- Passive responses remain unposted throughout rollout.

## 10. Deferred Work

- A statistical or LLM intent classifier for Level 3.
- Conversation-thread context for determining whether a question targets another member or the bot.
- Enabling passive Discord responses.
- Output-integrity and temporal-caveat changes tracked separately in GitHub issues #59 and #61.

## 11. Implementation Review Record

**Review date:** 2026-09-13  
**Result:** No remaining deviations after fixes.

Three independent code-quality passes and a replay of the rolling production
sample identified and corrected these deviations before deployment:

- Coordination, rhetorical, reported, and quoted questions could still be admitted by question syntax alone.
- Referral keywords could suppress legitimate questions about historical referral practices.
- Some unpunctuated knowledge questions were too narrowly recognized.
- Gate metadata did not survive both terminal workflow branches.
- Explicit advice wording could admit off-topic requests without TPM or historical-community evidence.
- Phase 9 retained an older instruction that conflicted with the precision-first contract.

The review re-ran after these fixes. The repository-specific
`docs/04_Coding_Agent_Rules/engineering_insights.md` checklist referenced by the
review procedure is not present in this repository, so its numbered-clause audit
was not applicable; the repository `AGENTS.md` rules were checked instead.

### As-built system view

```text
Discord -> listener -> n8n intake -> shared RAG core
                         |                 |
                         v                 v
                    Postgres trace     Qdrant/Gemini
                         |
                         v
                  shadow result only
```

### As-built component view

```text
Intake
  +-- normalize and capture
  +-- Level 1 exclusions
  +-- Level 2 evidenced admission
  +-- Level 3 fail-closed result
  +-- routing trace
  +-- optional shared-core call
```

### As-built code view

```text
Set Intake Active Call
  +-- ignore(level, reason, class, evidence)
  +-- admit(reason, evidence)
  +-- exclusion patterns
  +-- request + corpus-evidence rules
  +-- route fields
```

### As-built workflow view

```text
event -> duplicate check -> active/direct? -> bypass
                              |
                              no
                              v
                    L1 exclude? -> ignored
                              |
                              no
                              v
                    L2 admit? -> RAG shadow
                              |
                              no
                              v
                         L3 ignored
```
