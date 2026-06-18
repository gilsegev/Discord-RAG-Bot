# TPM Unite RAG Bot — Retrieval, Context & Prompt Contracts

**Owner:** Hemanth Aragonda · **Status:** Draft for review · **Related:** Arch Overview; Product Vision & Requirements; Evaluation & Feedback Scoring Design

## Purpose

This document defines three contracts that n8n needs before it can query Qdrant, assemble context, and generate answers. Without these contracts, n8n will retrieve "some chunks" but not necessarily the right ones, send unstructured context to the LLM, and produce answers with no consistent refusal or citation behaviour.

The three contracts are:
1. **Retrieval Contract** — how n8n queries Qdrant
2. **Context Assembly Contract** — what gets sent to the LLM
3. **Prompt/Response Contract** — how the bot answers and refuses

---

## 1. Retrieval Contract

### 1.1 Query text format

All queries sent to Qdrant must use the Nomic task-instruction prefix:

```
search_query: <user question text>
```

This prefix is not optional. Nomic Embed v1.5 was trained with task prefixes — omitting `search_query:` on queries while using `search_document:` on indexed chunks degrades retrieval quality. Documents in the index are already stored with `search_document:` prefix applied at embed time.

### 1.2 Embedding model

| Setting | Value |
|---|---|
| Model | `nomic-ai/nomic-embed-text-v1.5` |
| Vector size | 768 |
| Distance metric | Cosine |
| Normalization | Required — vectors are L2-normalized at index time |

### 1.3 Retrieval stages and score definitions

Two-stage retrieval — broad recall first, then rerank. Two separate scoring systems are in use and must not be confused:

| Term | System | Scale | Starting threshold | Usage |
|---|---|---|---|---|
| `retrieval_score` | Qdrant cosine similarity | 0.0 – 1.0 | 0.55 | Stage 1 — filters weak Qdrant results before reranking |
| `reranker_score` | CrossEncoder score | unbounded, typically -10 to +10 | > 0 (recalibrate against eval set) | Stage 2 — ranks Stage 1 candidates; proceeds to boost and dedupe |

For `cross-encoder/ms-marco-MiniLM-L-6-v2`, positive scores indicate relevance and negative scores indicate non-relevance. `reranker_score > 0` is a safe starting gate — recalibrate once the 40–60 question eval set is built.

**Stage 1 — Qdrant retrieval:**
- Query Qdrant with `top_k=20`
- Apply `retrieval_score >= 0.55` filter
- If fewer than 3 results pass the threshold — trigger refusal, do not proceed to reranking

**Stage 2 — CrossEncoder reranking:**
- Pass Stage 1 candidates to CrossEncoder `cross-encoder/ms-marco-MiniLM-L-6-v2`
- Rerank by `reranker_score`
- Apply starting threshold `reranker_score > 0`
- Candidates then proceed to reaction boost and dedupe before final top-5 selection (see section 1.6 for full pipeline order)

**What score appears in the context block sent to the LLM:**
The `reranker_score` is included in the context block (section 2.1), not the `retrieval_score`. The reranker score is the more meaningful signal at the point of context assembly.

### 1.4 Default search scope

By default, retrieval searches **all channels**. No channel filter is applied unless the user explicitly specifies one (e.g. "in #interview-prep").

Available metadata filters (applied as Qdrant payload filters):

| Filter | Field | Type | Example |
|---|---|---|---|
| Channel | `channel` | keyword | `"tpm-interview-resources"` |
| Thread | `thread_name` | keyword | `"Future of TPMs with AI"` |
| Date after | `start_ts` | datetime | `"2023-01-01T00:00:00"` |
| Date before | `end_ts` | datetime | `"2024-01-01T00:00:00"` |
| Max span | `span_days` | float | `<= 365` (exclude very long-span chunks for time-sensitive queries) |

### 1.5 Dedupe strategy for overlapping chunks

Two sources of duplication exist in the indexed corpus and both must be handled:

**Source 1 — Boundary overlap:** The ingestion pipeline uses 2-message overlap at chunk boundaries. Adjacent chunks share messages.

**Source 2 — Reply-chain root duplication:** Reply-aware chunking causes parent/root messages to appear in multiple chunks. Observed in the 13-file run: 1,014 message IDs appear in multiple chunks, max 5 appearances for a single message, 1,571 extra message memberships from overlap.

**Important: both dedupe rules must be applied AFTER reaction boost and BEFORE final top-5 selection.** The boost (section 1.6) must run first so that highly-reacted chunks are not discarded before their score is elevated. `boosted_reranker_score` referenced below is defined in section 1.6 — read section 1.6 before implementing dedupe.

**Dedupe rule 1 — Boundary overlap:**

After reaction boost, apply the following formula for each pair of candidate chunks. For `top_k=20` Stage 1 results this requires at most 190 pairwise comparisons — acceptable for synchronous n8n execution.

```
shared        = intersection(chunk_a.message_ids, chunk_b.message_ids)
overlap_ratio = len(shared) / min(len(chunk_a.message_ids), len(chunk_b.message_ids))
```

If `overlap_ratio > 0.5` — keep only the higher `boosted_reranker_score` chunk.

Using `min()` as the denominator catches cases where a short reply-chain chunk is mostly contained inside a larger chunk — Jaccard similarity would miss these cases.

**Note on split chunks:** Each split piece now stores only the `message_ids` actually rendered in that piece (fixed in ingestion v8). Dedupe correctly compares piece-level message sets, not the full original chunk's message set.

**Dedupe rule 2 — Reply-chain root duplication:**

If multiple retrieved chunks share the same root/parent message ID:
- Keep the highest `boosted_reranker_score` chunk
- Only retain an additional chunk if it contains meaningfully different child replies (i.e. `overlap_ratio <= 0.5` against the kept chunk)

**Current implementation note:** `root_message_id` is not yet stored in the Qdrant payload — deferred to PR #5 (see Open Question 7). Until it is added, n8n must include a dedupe placeholder after reaction boost and before context assembly that applies rule 1 formula across all candidate pairs. This placeholder must be in place before this PR is merged. Full reply-root dedupe upgrades automatically when `root_message_id` arrives in PR #5.

### 1.6 Reaction-based ranking boost

CaliMan flagged that replies with reactions should rank higher in retrieval. The boost is applied **after reranking and before dedupe** so that highly-reacted chunks are not discarded before the boost has any effect.

**Full pipeline order:**
```
Stage 1: Qdrant retrieval (top_k=20, retrieval_score >= 0.55)
    ↓
Stage 2: CrossEncoder reranking (reranker_score > 0)
    ↓
Stage 3: Reaction boost applied to reranker_score → boosted_reranker_score
    ↓
Stage 4: Dedupe (boundary overlap rule 1 + reply-root rule 2)
    ↓
Stage 5: Final top-5 selection for context assembly
```

**Boost formula:**
```
boosted_reranker_score = reranker_score * (1 + 0.1 * min(reaction_count, 5))
```

This caps the boost at 50% for chunks with 5 or more reactions.

**n8n implementation note:** `reaction_count` must be added to the Qdrant payload during ingestion before Stage 3 can be activated (see Open Question 3). Until `reaction_count` is available in the payload, treat it as 0 — do not error on missing field. Pass `reranker_score` directly to Stage 4 as `boosted_reranker_score` with no modification.

---

## 2. Context Assembly Contract

### 2.1 What gets sent to the LLM

For each of the top 5 reranked, boosted, and deduped chunks, include the following fields in the assembled context block.

**Note:** `channel_id`, `first_message_id`, and `message_ids` are stored in the Qdrant payload (added in ingestion v7, corrected per-piece in v8). `first_message_id` and `message_ids` reflect the actual messages rendered in each split piece — not the full original chunk's message set.

```
--- Context chunk {n} of {total} ---
Channel:      #{channel}
Thread:       {thread_name or "N/A"}
Date range:   {start_ts[:10]} to {end_ts[:10]}
Authors:      {comma-joined authors}
Score:        {reranker_score, 2 decimal places}
Message IDs:  {comma-joined message_ids}
Discord link: https://discord.com/channels/853099205206999050/{channel_id}/{first_message_id}

{chunk text}
```

Fields rationale:
- **Channel + Thread** — grounds the LLM in where the conversation happened
- **Date range** — lets the LLM note if context is old; critical for fast-changing topics like hiring
- **Authors** — preserves community attribution; supports citation style
- **Score** — `reranker_score` (not `retrieval_score`) — the more meaningful confidence signal at context assembly time
- **Message IDs** — enables dedupe detection and feedback correlation; reflects actual messages in this piece
- **Discord link** — constructed from `channel_id` and `first_message_id` stored in Qdrant payload; points to the correct first message in each split piece

### 2.2 Token budget

| Allocation | Tokens |
|---|---|
| System prompt | ~300 |
| Retrieved context (5 chunks × avg 154 tokens per chunk based on current index) | ~770 |
| Context block metadata overhead (5 chunks × ~50 tokens per header) | ~250 |
| User question | ~100 |
| Answer generation headroom | ~800 |
| **Total budget** | **~2,220 tokens** |

The 154 tokens/chunk figure is derived from the current Qdrant index average (153.6 tokens/chunk across 1,232 indexed chunks). The ~50 tokens/header estimate accounts for channel, thread, date range, authors, score, message IDs, and Discord link.

### 2.3 Context overflow handling

**Single chunk overflow:** The current largest chunk is 692 tokens — well under the 1,200-token context budget for 5 chunks. The ingestion pipeline's `_split_if_needed()` function splits oversized chunks at line boundaries before indexing, including a single-line overflow guard (v8). n8n does not need to handle single-chunk overflow at retrieval time.

**Multi-chunk overflow:** If the assembled 5 chunks exceed the 1,200-token context budget:
1. Drop the lowest `reranker_score` chunk
2. Repeat until under budget
3. If fewer than 3 chunks remain after dropping, trigger refusal — "insufficient context after token budget constraints"
4. Log the overflow event to the observability layer with chunk count and token counts

---

## 3. Prompt / Response Contract

### 3.1 System prompt

```
You are an assistant for the TPM Unite Discord community.
You have access to retrieved excerpts from real TPM Unite conversations
spanning the community's history.

RULES — follow exactly, in priority order:

1. GROUNDING: Answer ONLY from the provided context blocks.
   Never use general knowledge, personal opinions, or external sources.
   Every claim must be traceable to a specific context block.

2. REFUSAL: If the provided context is insufficient to answer confidently,
   respond with exactly the refusal text defined in section 3.2.
   Then stop. Do not add caveats, partial answers, or suggestions.

3. CITATION: After each key claim, cite the source in this format:
   (#{channel}, {YYYY-MM-DD})
   For forum threads: (#{channel} > {thread_name}, {YYYY-MM-DD})

4. FRAMING: Frame answers as community wisdom, not authoritative rulings.
   Use language like "TPM Unite members have discussed...",
   "The community has shared...", "Past discussions suggest..."
   Never say "You should..." or "The answer is..."

5. TEMPORAL HONESTY: If context is older than 12 months, note it:
   "Note: this reflects TPM Unite discussions from {year} —
   the community may have more recent context."

6. NUANCE CAVEAT: For subjective, evolving, or role-specific topics,
   close with:
   "This reflects past TPM Unite discussions — the community may have
   more recent or personal context to add."

7. SAFETY: Do not surface personal identifying information from the
   context even if present. Do not answer questions that require
   advice beyond what the community has discussed.
```

### 3.2 Refusal text

Exact single-line string — do not paraphrase, do not split across lines in implementation:

```
I don't have enough TPM Unite specific context to answer this confidently, try rephrasing or ask the community directly.
```

**Implementation note:** This must be a single unbroken string in the n8n node and LLM prompt. The evaluation rubric in `evaluation-and-feedback-scoring-design.md` checks this string exactly. A newline in the middle of the string counts as a variation and fails the tone/refusal dimension. When rendering in Discord, the string may wrap visually — that is fine. The underlying string must have no embedded newline.

### 3.3 Source and citation style

| Situation | Citation format |
|---|---|
| Normal channel | `(#tpm-interview-resources, 2023-06-15)` |
| Forum thread | `(#forum-discussion > Future of TPMs with AI, 2023-06-15)` |
| Multiple sources | List each on a new line after the answer |
| No citable source | Do not cite — trigger refusal instead |

### 3.4 Uncertainty handling

Two separate score signals apply at different pipeline stages:

| Signal | Score type | Threshold | Handling |
|---|---|---|---|
| `retrieval_score < 0.55` for all Stage 1 results | Qdrant cosine similarity | 0.55 | Trigger refusal — no context found |
| `reranker_score <= 0` for all top 5 chunks | CrossEncoder score | 0 (starting gate) | Trigger refusal — context not relevant |
| `0 < reranker_score < 2` for all top 5 chunks | CrossEncoder score | 2 (weak signal) | Add "Note: retrieved context may not be a strong match for this question." before the answer |
| All chunks older than 12 months | Date metadata | — | Add temporal honesty note (Rule 5) |
| `span_days > 365` on retrieved chunks | Payload field | — | Add "Note: this conversation spans a wide time range — context may reflect different community views over time." |
| Subjective/evolving topic | Content signal | — | Add nuance caveat (Rule 6) |

### 3.5 Whether to mention "based on TPM Unite history"

Yes — always frame answers with explicit TPM Unite attribution. This prevents the bot from sounding like a generic AI assistant and reinforces that answers come from real community discussions.

### 3.6 Whether to include channel/thread references

Yes — always include channel and thread name in citations. This lets members follow up in the original channel and validates that the answer comes from a real discussion.

### 3.7 Whether to invite members to continue discussion

Yes — for nuanced, evolving, or role-specific answers, close with the nuance caveat (Rule 6). This invites the community to add more recent context and prevents the bot from being treated as a final authority.

### 3.8 Safety rule

The bot must not answer beyond what is in the retrieved context. This is Rule 1 in the system prompt and is the non-negotiable gate in the evaluation rubric. The v1 bot was deprecated specifically for violating this rule. Any answer that adds claims not present in the retrieved context is a groundedness failure regardless of how helpful it reads.

---

## Open Questions for the Team

1. **Reranker model** — CrossEncoder `cross-encoder/ms-marco-MiniLM-L-6-v2` is the proposed default. Does the team want to evaluate alternatives before locking this in?

2. **Reranker score thresholds** — starting values proposed: `> 0` for refusal gate, `> 2` for weak-signal note. Calibrate both against the 40–60 question eval set. Who owns calibration?

3. **Reaction-based ranking boost** — formula defined in section 1.6. Requires `reaction_count` field added to Qdrant payload during ingestion. n8n must treat missing `reaction_count` as 0 — no boost applied, no error. Team to ratify formula and confirm ingestion update ownership before implementation.

4. **Discord link construction** — guild ID `853099205206999050` hardcoded. `channel_id` and `first_message_id` now in Qdrant payload (ingestion v8 — corrected per-piece). Confirm guild ID is stable — server migration invalidates all stored links.

5. **LLM selection** — Arch Overview specifies Gemini API as current cognitive engine. This document is model-agnostic. Token budget allocations may require adjustment if model changes.

6. **Passive listener retrieval** — should passive queries use a higher `retrieval_score` threshold (e.g. 0.70) to reduce noise responses?

7. **root_message_id in payload (deferred to PR #5)** — full reply-root dedupe (rule 2 in section 1.5) requires `root_message_id` in the Qdrant payload. This field is deferred to PR #5. Before this PR is merged, n8n must implement a dedupe placeholder using rule 1 formula (`message_ids` overlap) after reaction boost and before context assembly. Full reply-root dedupe activates automatically when `root_message_id` arrives in PR #5 — no n8n rework required, only the ingestion update.

8. **Context token budget recalibration** — 154 tokens/chunk average based on current 13-file dataset. Recalibrate after each major data expansion.