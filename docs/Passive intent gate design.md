# Passive Gemini Post-Decision Design

**Status:** Implemented; one production shadow non-question verified on 2026-09-18

**Owner:** Discord RAG Bot maintainers
**Related:** Phase 9; Retrieval, Context & Prompt Contracts; Observability Design

## Purpose and decision

The prior three-level, regex-heavy passive gate suppressed legitimate questions about referrals, community calls, contacts, and interview preparation. The problem is not whether a message contains a question mark; it is whether an unsolicited bot response would usefully address a member request. Mechanical intake checks must not make that semantic judgment.

```text
Discord event
  -> four mechanical checks
  -> shared retrieval and Gemini generation
  -> structured Gemini post decision
  -> grounding, citation, output-integrity, and posting guards
  -> Discord post only when all guards approve
```

This reuses the existing Gemini call; there is no separate intent-model call. Passive responses stay in Postgres-only shadow mode until a separate authorization enables posting. Active calls and retrieval-only regression keep their existing generation contracts.

The shared retrieval gate still applies before Gemini. When reranking or context assembly finds no usable evidence, the passive transaction records the specific retrieval refusal, leaves the answer empty, sets `should_post=false`, and makes no Gemini or Discord call. A passive message with usable context reaches Gemini for the structured post decision. The final Discord gate requires an explicit `should_post=true` for passive traffic.

## Four early mechanical exclusions

For ordinary passive traffic, only these checks may stop a message before retrieval:

1. **Duplicate event:** The same Discord guild/message ID was delivered again; a durable intake-event claim is independent of corpus-capture eligibility. Identical text in a new message ID is not a duplicate.
2. **Non-member event:** Bot-authored, webhook-authored, or system-generated events.
3. **Empty content:** No usable message text after normalization.
4. **Excluded channel:** The channel ID is on an explicit configured exclusion list.

The passive-enabled switch remains an operational kill switch, not a content classification rule. No length threshold, URL-only rule, reply rule, job/referral rule, schedule rule, keyword list, or question-syntax rule may reject meaningful member text at intake. Direct mentions still use the active-call path after event-level checks. The older Stage 0 pre-retrieval privacy gate remains for active calls but is bypassed for passive calls; passive Gemini must instead decline answers requiring personal contact or identifying information.

## Gemini post-decision contract

For passive messages, the existing final Gemini request receives the original message and retrieved context. It must decide intent before drafting. Its JSON schema is:

```json
{
  "should_post": false,
  "intent": "non_request",
  "decision_reason": "The member shared an experience but did not ask for help.",
  "final_answer": ""
}
```

- `intent` is `request`, `non_request`, or `unclear`.
- `should_post` is true only for an explicit or implied request for help, advice, information, referral-process guidance, community resources, or a grounded answer; a question mark is not required.
- Statements, anecdotes, acknowledgements, advertisements, rhetorical questions, and replies solely to another member receive `should_post=false`.
- If intent is unclear, context cannot support a useful answer, or the answer would need private contact information, return `should_post=false` and an empty answer.
- Referral and community-resource questions are valid requests; safety limits what can be said, not whether Gemini examines them.
- `final_answer` is a Discord-ready, grounded, cited answer only when `should_post=true`; otherwise it is empty.
- `decision_reason` is short diagnostic text, not user-facing output.

The workflow validates the exact schema and requires `should_post=true`, `intent=request`, usable retrieved context, a nonempty answer, citation validation, and output-integrity validation. Missing, malformed, contradictory, or unclear output fails closed. The n8n posting condition independently checks the validated `should_post` field. The decision is stored with generation metadata for audit. No raw message is added to trace metadata.

For passive traffic with no usable retrieval context, Gemini still makes the intent decision using an empty context and must decline posting; this lets us distinguish a valid question with insufficient corpus evidence from a non-request. This adds Gemini cost but does not add a second call. Existing active-call and regression behavior does not change.

## Rollout and acceptance

1. Unit-test the four mechanical checks and structured decision parser, including malformed output and contradictory decisions.
2. Push only the intake and shared-core workflows with repository sync scripts after comparing remote versions.
3. Run a non-question through the production passive route with Discord posting disabled; verify Gemini returned `non_request`, `should_post=false`, and no Discord post.
4. Run a genuine request through the same route and verify it reaches Gemini as `request`; keep the answer unposted during shadow evaluation.
5. Replay and label the 192-message historical set; calculate false-ignore and false-admit rates before considering live passive posting.

**Pass criteria:** The non-question reaches Gemini but is not posted; valid requests are not mechanically dropped; malformed model output cannot post; active and retrieval-only regression paths remain unchanged. Posting passive answers requires a separate review and approval.

## Previous design

The September 13 three-level deterministic design (`passive-intent-v1`) is superseded. Its regex exclusions and knowledge-request admission rules were too brittle: among the reviewed ignored messages were valid contact, referral, scheduling, and interview-advice questions. The prior implementation-review record remains in Git history; this document describes the replacement contract and current verification status.

## Verification record

The Terra Medium production probe submitted a historical Think-Cell statement through passive intake with Discord posting disabled. After review fixes, transaction `7f9339a5-9590-4892-a708-d0e3e2a69855` reached Gemini. Gemini returned `intent=non_request`, `should_post=false`, and a reason that the member was sharing a statement rather than requesting help. The durable record shows `status=refused`, `response_status=not_posted`, no Discord response message ID, `final_response_text=null`, `citation_guard_failed=false`, and 1,205 total Gemini tokens. This verifies the non-question suppression journey, not population-level precision or recall; those still require the labeled historical replay.

A separate production retry check used a corpus-ineligible two-character message with the same Discord message ID twice. The first delivery was a passive candidate; the second returned `ignored` with `duplicate_event`. A different message ID remains processable by the durable claim contract. No test call was allowed to post to Discord.

## Implementation review record

Three independent code-quality passes found four defects in the initial implementation: passive wording appeared in active prompts; empty non-request output was incorrectly marked as a citation failure; the earlier privacy gate could prevent passive contact/referral questions from reaching Gemini; and duplicate detection depended on corpus-capture eligibility. All four were fixed before the final production probe. The shared active-call privacy gate remains unchanged, while passive contact questions reach Gemini under the no-personal-details answer rule. A durable event claim now deduplicates the same Discord message ID even when the message is not eligible for corpus ingestion. The repository-specific `docs/04_Coding_Agent_Rules/engineering_insights.md` file referenced by the implementation-review skill is absent; current `AGENTS.md` was used instead. No known deviation remains from the requested four-check design. The historical recall study and live passive posting authorization remain intentionally open.

```text
System: Discord -> listener -> n8n -> Qdrant -> Gemini
                    |         |                 |
                    v         v                 v
                event ID   Postgres          post decision
```

```text
Components: intake claim -> four checks -> shared core
                                       -> Gemini decision
                                       -> posting guard
```

```text
Code: intake route -> context prompt -> JSON parser
                                       -> validated should_post
                                       -> not-posted/post branch
```

```text
Journey: member statement -> retrieval -> Gemini says no
                             -> audit metadata -> no Discord post
```
