# TPM Unite RAG Bot — Consent-Based Referral Registry Design

**Owner:** CtrlAltDeliver · **Status:** Draft for review · **Related:** Retrieval, Context & Prompt Contracts (§3.2 refusal contract); Passive intent gate design; Evaluation & Feedback Scoring Design

## Problem

When a member asks the bot for a referral — *"Can someone provide a referral to the Apple team here?"* — the bot returns a relevant but **non-responsive** answer. It describes how referrals work in the community (post in #referrals, coordinate via DM) instead of answering the request the user actually made: *connect me to a person who can refer me.*

This is not a retrieval-quality bug. Retrieval is grounded and cited. The mismatch is structural: **two different requests hide in one phrasing.**

1. **Knowledge** — "How do referrals work here / has Apple come up?" RAG answers this well. It is the answer users currently get.
2. **Action** — "Find me a specific person who will refer me." RAG cannot answer this safely, because the useful answer ("member @X works at Apple") is an identity disclosure — associating an identifiable member with an employer and volunteering them for solicitation without their consent. That is the exact boundary the SAFETY refusal class exists to hold (see the refusal contract §3.2).

So the privacy-safe answer to the action request collapses back into the generic "post in #referrals" — which is why the bot looks unhelpful. The bot is behaving correctly; it is missing an **intent for the request and an action path for it.**

## Scope

This document specifies the **consent-based referral table** and the deterministic lookup that pulls a name from it. The recognition-and-routing upstream of the lookup — how a referral request is detected at the start of the chain, how the company is extracted and normalized, and how it reaches the table before the RAG path while preserving posting permissions and duplicate prevention — is specified in [Referral-Request Recognition & Routing](referral-request-routing-design.md). The two docs together are the full, implementation-ready design.

- **In scope (this doc):** the consent model, data model, the deterministic lookup, opt-in/opt-out/expiry flows, and the privacy invariants.
- **Specified in the linked routing doc:** deterministic recognition, company extraction/normalization, active vs passive handling, edge cases, and the pre-RAG routing.
- **Out of scope (both docs):** auto-brokering (the bot contacting a referrer on a user's behalf); any corpus/ingestion change.

## Rollout

The table, the opt-in command, and the deterministic lookup are built together as one feature. Two facts about how it comes to life, neither a reason to defer:

- **It launches empty, and that is fine.** On day one the table has no rows, so every lookup misses and returns the generic "post in #referrals" reply. That generic reply is simply the empty-result path — not a separate phase. The feature is correct from the first deploy; it just has nothing to surface yet.
- **It fills by demand, not by seeding.** The table is never populated from the corpus (Principle 1). It fills through opt-in, driven by the discovery loop in the consent model: every referral response advertises the opt-in. Hit-rate is logged from day one (see the routing doc's observability line), so the value of the feature is measurable as the table fills.

A thin first iteration is fine (see MVP); the re-confirmation DM and optional fields can follow. But the lookup against the table is the core behavior and ships in the first iteration.

## Principles

Three invariants hold at all times. Everything below serves them.

1. **Opt-in only.** A registry row exists because a member created it — never because the bot inferred employer from message history. No corpus-derived population, ever.
2. **Purpose-limited.** Registry data answers referral requests and nothing else. It is never embedded into the RAG corpus and never used to answer other questions.
3. **Firewalled from the Q&A path.** The referral lookup bypasses retrieval / rerank / Gemini entirely. If registry identity could leak into a normal answer, the PII boundary is defeated through the back door.

## Consent model (the core of this design)

**At ~7,000 members, consent is self-selecting — it is collected from the few dozen willing to refer, not from the whole server.** 7k is the *discovery* audience (who learns the feature exists), not the *consent* audience (who records an offer). These are separate problems.

### How consent is collected

1. **The act is private, self-serve, and self-only.** A member opts themselves in via a private/ephemeral interaction with the bot (slash command with an ephemeral response, or a DM). It writes the **invoker's own** member ID — a member can never volunteer a colleague. It does not broadcast the member's employer to the server.
2. **Privacy is the feature.** Today, offering a referral means publicly posting *"I work at Apple, DM me"* to ~7k people. The registry lets a member offer referrals **without** publicly disclosing their employer: the offer is revealed only to the specific person who asks about that company, through the contact method the member chose. This is strictly better than the status quo and is why a privacy-conscious senior member would use it.
3. **Discovery is demand-driven.** The primary channel is the bot itself: every referral response appends one line — *"If you're willing to give referrals, opt in with `/refer-offer`."* Each request to **receive** a referral advertises the ability to **give** one. Supplemented by a single pinned announcement in #referrals. No mass pings.
4. **Cold-start is intentional.** The table launches empty; the generic "post in #referrals" reply is the response on every no-match, which is also the day-one state. It fills organically via (3). It is never seeded from the corpus.

### Consent lifecycle

| Rule | Why |
|---|---|
| **Offers expire (TTL ~6 months) and require re-confirmation** | Members change jobs. A stale "I refer at Apple" from someone who has left is both wrong and embarrassing. Near expiry, the bot DMs "still offering Apple referrals?" |
| **Opt-out is immediate and complete** | One command sets the row inactive; the bot stops surfacing it that second. |
| **The bot surfaces; the member initiates contact** | The bot says "@X opted in — reach them via DM." It does **not** contact the referrer on the user's behalf. Auto-brokering is a separate, heavier consent design, explicitly out of scope. |

## Data model

A single Postgres table (existing Postgres on Railway — no new infra class):

```sql
CREATE TABLE referral_offers (
  offer_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  member_id       text        NOT NULL,              -- real Discord user ID, NOT hashed
  company         text        NOT NULL,              -- canonicalized: "Apple", "NVIDIA"
  contact_pref    text        NOT NULL,              -- 'dm' | 'post_in_referrals' | 'mention_ok'
  note            text,                              -- optional, e.g. "senior roles only"
  status          text        NOT NULL DEFAULT 'active',  -- 'active' | 'withdrawn'
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL               -- now() + 6 months
);
CREATE INDEX idx_referral_offers_lookup ON referral_offers (company, status, expires_at);
```

One deliberate departure from the eval tables: **`member_id` is the real Discord ID, not hashed.** In the eval pipeline identity is incidental, so it is hashed. Here identity *is the product* — the member consented to be identified for this purpose. That is precisely why purpose-limitation (Principle 2) is non-negotiable: this is the one place real identity lives, so it must be the most tightly scoped data in the system.

## Flows

### Opt-in
```
/refer-offer company:Apple contact:dm note:"senior roles"
  -> bot canonicalizes company
  -> INSERT row (member_id = invoker, status active, expires_at = now()+6mo)
  -> ephemeral confirmation to the invoker only
```

### Lookup / match (the deterministic flow)
```
referral request recognized with a company (see routing doc)
  -> company already canonicalized upstream (shared alias map, e.g. NVDA->NVIDIA)
  -> SELECT ... WHERE company = ? AND status = 'active' AND expires_at > now()
  -> matches:    respond with count + how to reach each, per contact_pref
  -> no matches: generic response (post in #referrals)
```

This runs as a Stage 0 branch **parallel to** the RAG core — not through it. That decoupling is what keeps the identity data off the Q&A path (Principle 3).

### Opt-out / expiry
```
/refer-withdraw company:Apple   -> status = 'withdrawn' (immediate)
near expires_at                 -> bot DMs re-confirm prompt; no response -> lapses to inactive
```

## Privacy invariants (acceptance criteria)

- [ ] Only `status='active'`, unexpired rows are ever returned.
- [ ] Rows are created only by the member themselves (invoker ID == member_id).
- [ ] Registry data is never written into the corpus and never reachable from the RAG answer path.
- [ ] Opt-out takes effect immediately and completely.
- [ ] Offers expire on TTL and require explicit re-confirmation to persist.
- [ ] The bot never contacts a referrer on a requester's behalf.

## MVP

First iteration that works end to end:
- the `referral_offers` table
- `/refer-offer` + `/refer-withdraw`
- the deterministic lookup wired into the referral route (see routing doc)
- a flat 6-month `expires_at` (manual; no re-confirmation DM yet)

Deferred to a later iteration: the re-confirmation DM, the `note` field, `mention_ok` as a contact option.

## Open decisions (for Gil)

1. **Opt-in surface.** Slash command (needs interaction handling — does the merged Discord Bot API dispatch already give us this?) vs. a lower-code structured-reply channel that n8n ingests. Leaning slash command for clarity and auditability.
2. **Company canonicalization.** Factor a shared alias/canonicalization utility used by both retrieval (query-rewriting) and this lookup, rather than duplicating. Who owns it?
3. **Re-confirmation cadence.** 6-month TTL assumed; confirm.
