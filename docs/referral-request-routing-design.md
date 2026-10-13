# TPM Unite RAG Bot — Referral-Request Recognition & Routing

**Owner:** CtrlAltDeliver · **Status:** Draft for review · **Related:** [Consent-Based Referral Registry Design](referral-registry-design.md) (the table this routes to); Passive intent gate design; Retrieval, Context & Prompt Contracts (§3.2 refusal contract / Stage 0)

## Purpose

Incoming referral requests must be **identified at the start of the RAG chain** and routed to a **deterministic flow that pulls a name from the referral table** — before retrieval, reranking, or Gemini run. This document specifies that recognition-and-routing contract. The table and its consent model are specified in the [Consent-Based Referral Registry design](referral-registry-design.md); this document is everything upstream of the lookup.

The design goal is a deterministic path for a request like *"Can someone refer me to Apple?"*: recognize it, extract and normalize the company, and hand a company key to the table lookup — with no LLM call on that path and a safe fallback when anything is uncertain.

## Where recognition runs — active vs passive

The pipeline treats the two routes asymmetrically (see Passive intent gate design), and this contract respects that.

| | Active call (@mention) | Passive (ambient message) |
|---|---|---|
| Pre-retrieval intent pass | **Yes** — the Stage 0 gate (§3.2) | **No** — four mechanical checks only, by design |
| Referral recognition | **Deterministic, at Stage 0, before retrieval** | Not routed to the table (see below) |
| Where a referral request is answered | The deterministic table-lookup flow | The existing Gemini post-decision, shadow-only |

**Active path.** A deterministic referral recognizer runs at **Stage 0, before retrieval** — the start of the chain. On a positive match it routes straight to the table lookup and the transaction completes without retrieval/rerank/Gemini. This is where referral requests actually arrive: a member wanting a referral @mentions the bot.

**Passive path — deliberately not table-routed.** Passive intake is contractually limited to four mechanical checks (duplicate / non-member / empty / excluded-channel); no content rule may act there, and the regex intent gate (`passive-intent-v1`) was retired as too brittle. Passive is also shadow-only — it does not post. So wiring a referral table lookup into passive intake would both break that contract and cross the passive-posting boundary. Passive referral mentions therefore keep their current behavior (Gemini post-decision, which already treats referral questions as valid requests under the no-personal-details rule). **Unifying recognition across both routes would require revising the passive intake contract — a larger change, called out in Decisions below rather than assumed here.**

## Why recognition is deterministic here (and why that is not the retired gate)

Gil's requirement is a deterministic flow. The team retired deterministic regex intent gating on passive traffic as too brittle — so this needs reconciling, and the reconciliation is the design's core safety argument:

- `passive-intent-v1` was brittle because it decided **"should the bot respond at all?"** for *every* passive message — an open-ended semantic judgment where a deterministic miss **suppressed a legitimate answer entirely**.
- This recognizer decides a far narrower question on **explicit active @mentions** only: *"is this a referral request, and for which known company?"*
- **Both failure directions are soft.** A miss → the message falls through to the normal RAG path and gets a knowledge answer (fine). A false hit → the table lookup finds nothing and returns the generic #referrals reply (fine). The recognizer never surfaces a member's identity on its own — it only ever triggers a lookup against the consented table.

Because the failure mode degrades to a reasonable answer instead of killing one, deterministic recognition is appropriate *here* even though it was wrong *there*.

## How recognition works (deterministic, two parts)

A referral request is recognized only when **both** fire:

1. **Referral-intent trigger** — the message (after stripping the bot mention) matches a bounded, maintained set of referral phrasings: *refer me*, *referral to/at/for*, *can someone refer*, *anyone at … who can refer*, *looking for a referral*, etc. A small, reviewable rule set — not open-ended request detection.
2. **Known-company match** — the message contains a company present in the shared company-canonicalization dictionary (the same alias map retrieval uses for query rewriting, e.g. `NVDA → NVIDIA`, `Meta/Facebook`). **The dictionary is the extractor**: company "extraction" is a lookup against known companies, not NLP entity recognition. The matched canonical name is the table-lookup key.

This keeps the whole recognition step deterministic and bounded, and reuses the alias utility rather than inventing a referral-specific one.

## Edge cases — fail toward RAG, never toward the table

Governing rule: **a recognition failure must never cause an identity disclosure.** Every uncertain case fails *away* from the table.

| Case | Behavior |
|---|---|
| **Trigger fires, no known company** ("can anyone here refer me somewhere?") | Route to the referral response with **no company** — generic "post in #referrals with your target company + role." No table lookup (nothing to key on). |
| **Trigger fires, >1 known company** | Generic referral response that asks the member to specify a single company. Do not guess a key. |
| **No trigger** (incl. knowledge questions about referrals) | Fall through to the normal path — Stage 0 PII gate, then RAG. A knowledge answer is the safe, useful default. |
| **Recognizer error** | **Fail closed to the normal path.** Never default into the table route — a wrong table route is the one failure mode that could surface a member's identity. |

## Acceptance examples

| Message | Recognized as | Response |
|---|---|---|
| "Can someone refer me to Apple?" | referral request, company=`Apple` | Deterministic table lookup on `Apple`: return opted-in referrer(s) per their contact preference, or the generic #referrals reply if none. |
| "How do referrals work at Apple?" | **not** a referral request (no action trigger; it is a knowledge question) | Normal RAG answer, grounded and cited — unchanged from today. |
| "I got an Apple referral yesterday." | **not** a request (statement) | Active: no trigger → normal path, nothing actionable. Passive: Gemini post-decision returns `intent=non_request`, `should_post=false` (matches the verified Think-Cell statement probe). No table routing either way. |
| "Can anyone here refer me somewhere?" | referral request, company=`∅` | Generic referral response, no company, no lookup. |
| (recognizer errors on any of the above) | fail-closed | Normal RAG path; never the table route. |

The first three are exactly the person-vs-knowledge-vs-statement distinction Gil asked to see pinned down.

## Order of operations — reaching the table before RAG, with posting and dedup preserved

```text
Discord @mention
  -> durable intake-event claim (dedup by guild/message ID)        [unchanged]
  -> Stage 0:
       deterministic referral recognizer (trigger + known company)
          match -> DETERMINISTIC TABLE LOOKUP (SELECT ... WHERE company=? ...)
                     -> hit:  return referrer(s) per contact_pref
                     -> miss: generic #referrals reply
                   (returns here; retrieval/rerank/Gemini never run)
       else PII/safety gate (§3.2) -> SAFETY refusal
       else -> retrieval -> rerank -> dedupe -> Gemini             [normal RAG]
```

- **At the start of the chain:** recognition and the table lookup both sit at Stage 0, before retrieval — satisfying "identified at the beginning of the RAG chain and routed to a deterministic flow."
- **Deterministic lookup:** the table pull is a single indexed SQL query (`company`, `status='active'`, `expires_at > now()`), specified in the registry design. No model call on this path.
- **Firewalled from Q&A:** a referral request completes in the Stage 0 branch without retrieval/rerank/Gemini — so consented identity data never touches the RAG answer path (registry design, Principle 3).
- **Dedup preserved:** the durable intake-event claim runs *first*; a redelivered message ID is dropped (`duplicate_event`) before Stage 0, so a referral request can never be double-routed.
- **Posting preserved:** the referral reply is an active-call response and posts through the existing active-call path — no new posting surface, no change to the passive `should_post` shadow gate.
- **Observability:** each referral-routed transaction records `route=referral_request`, the normalized company (or `∅`), and hit vs. generic-fallback — so volume and hit-rate are measurable from day one.

## Decisions (for Gil, async)

1. **Passive scope.** Recognition is active-path only, for the contract reasons above. Unifying it onto passive would mean revising the four-mechanical-checks intake contract and the passive-posting boundary — flagging as his call, not assumed.
2. **Company-canonicalization ownership.** The alias/normalization dictionary is shared with retrieval's query rewriting. Factor once — who owns it and where does it live so both import it?
3. **Trigger rule-set maintenance.** The referral-phrasing rule set is small but needs an owner and a place to live (config, not code, so it is editable without a deploy).
