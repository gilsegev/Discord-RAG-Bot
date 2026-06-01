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

### 1.3 Retrieval stages

Two-stage retrieval — broad recall first, then rerank:

| Stage | Parameter | Value | Rationale |
|---|---|---|---|
| Stage 1 | `top_k` initial retrieval | 20 | Wide net to ensure relevant chunks are in the candidate set |
| Stage 2 | Reranked final context | 5 | CrossEncoder reranker scores Stage 1 results; top 5 passed to LLM |
| Fallback | Minimum score threshold | 0.55 | Cosine similarity below 0.55 = insufficient context; trigger refusal |

If Stage 1 returns fewer than 3 results above the 0.55 threshold, treat as no-context and trigger the refusal path. Do not pass weak results to the LLM.

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

The ingestion pipeline uses 2-message overlap at chunk boundaries. This means adjacent chunks share messages. To avoid surfacing near-duplicate context to the LLM:

1. After Stage 1 retrieval, compare `message_ids` arrays across results
2. If two chunks share > 50% of their `message_ids`, keep only the higher-scoring one
3. Apply deduplication before reranking

### 1.6 Behavior when results are weak

| Condition | Action |
|---|---|
| 0 results above 0.55 | Trigger refusal — no context found |
| 1–2 results above 0.55 | Trigger refusal — insufficient context |
| 3–20 results above 0.55 | Proceed to reranking |
| All results have `span_days > 365` | Add `[Note: retrieved context spans a wide time range — may reflect outdated community views]` to the assembled context header |

---

## 2. Context Assembly Contract

### 2.1 What gets sent to the LLM

For each of the top 5 reranked chunks, include the following fields in the assembled context block:

```
--- Context chunk {n} of {total} ---
Channel:      #{channel}
Thread:       {thread_name or "N/A"}
Date range:   {start_ts[:10]} to {end_ts[:10]}
Authors:      {comma-joined authors}
Score:        {reranker score, 2 decimal places}
Message IDs:  {comma-joined message_ids}
Discord link: https://discord.com/channels/853099205206999050/{channel_id}/{first_message_id}

{chunk text}
```

Fields rationale:
- **Channel + Thread** — grounds the LLM in where the conversation happened; helps it frame answers as community-specific
- **Date range** — lets the LLM note if context is old; critical for fast-changing topics like hiring
- **Authors** — preserves community attribution; supports citation style
- **Score** — gives the LLM signal on confidence; low scores should reduce answer assertiveness
- **Message IDs** — enables feedback correlation back to source messages in the observability layer
- **Discord link** — constructible from guild ID + channel ID + first message ID; allows the bot to link users back to the original discussion

### 2.2 Token budget

| Allocation | Tokens |
|---|---|
| System prompt | ~300 |
| Retrieved context (5 chunks × avg 154 tokens per chunk based on current index) | ~770 |
| Context block metadata overhead (5 chunks × ~30 tokens per header) | ~150 |
| User question | ~100 |
| Answer generation headroom | ~800 |
| **Total budget** | **~2,120 tokens** |

The 154 tokens/chunk figure is derived from the current Qdrant index average (153.6 tokens/chunk across 1,232 indexed chunks). If assembled context exceeds 1,200 tokens after 5 chunks, drop the lowest-scoring chunk and note the omission.

### 2.3 Context overflow handling

If the top 5 reranked chunks exceed the 1,200-token context budget:
1. Drop the lowest-scoring chunk
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
   respond with exactly:
   "I don't have enough TPM Unite specific context to answer this
   confidently, try rephrasing or ask the community directly."
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

Exact string — do not paraphrase:

```
I don't have enough TPM Unite specific context to answer this confidently,
try rephrasing or ask the community directly.
```

This exact string is checked by the evaluation rubric in `evaluation-and-feedback-scoring-design.md`. Any variation fails the tone/refusal dimension.

### 3.3 Source and citation style

| Situation | Citation format |
|---|---|
| Normal channel | `(#tpm-interview-resources, 2023-06-15)` |
| Forum thread | `(#forum-discussion > Future of TPMs with AI, 2023-06-15)` |
| Multiple sources | List each on a new line after the answer |
| No citable source | Do not cite — trigger refusal instead |

### 3.4 Uncertainty handling

| Signal | Handling |
|---|---|
| Reranker score < 0.6 for all chunks | Add "Note: retrieved context may not be a strong match for this question." before the answer |
| All chunks older than 12 months | Add temporal honesty note (Rule 5) |
| `span_days > 365` on retrieved chunks | Add "Note: this conversation spans a wide time range — context may reflect different community views over time." |
| Subjective/evolving topic | Add nuance caveat (Rule 6) |

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

2. **Score threshold (0.55)** — this is a starting value based on general RAG practice. It should be calibrated against the 40–60 question eval set once it is built. Who owns calibration?

3. **Reaction-based ranking boost** — CaliMan flagged that replies with reactions should rank higher. Proposed: add a `reaction_count` field to the Qdrant payload at index time, and apply a ranking boost of `score * (1 + 0.1 * min(reaction_count, 5))` after reranking. Team to ratify before implementation.

4. **Discord link construction** — the guild ID `853099205206999050` is hardcoded above. Confirm this is stable and does not change. If the server is ever migrated, all stored Discord links become invalid.

5. **LLM selection** — the Arch Overview specifies the Gemini API as the current cognitive engine. This document is written to be model-agnostic — the system prompt rules, refusal text, and citation format apply equally to any LLM. If the model changes, the prompt contract does not need to change but the token budget allocations may require adjustment based on the new model's context window.

6. **Passive listener retrieval** — the Arch Overview describes a passive listener path that monitors channel traffic without a direct bot mention. Should the retrieval contract apply equally to passive listener queries, or should passive queries use a higher score threshold (e.g. 0.70) to reduce noise responses?

7. **Context token budget recalibration** — the 154 tokens/chunk average is based on the current 13-file dataset. As more channels are exported and indexed, this average may shift. The token budget table should be recalibrated after each major data expansion.
