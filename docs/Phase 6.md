# Phase 6: Post-Rerank Dedupe
**Status:** Implementation in progress
**Scope:** Remove overlapping retrieval evidence after reranking and before top-five context selection.
**Related:** n8n Execution Plan, Retrieval Context Prompt Contracts, Architecture Overview, Observability Design

## Purpose
Phase 6 reduces repeated evidence caused by chunk-boundary overlap and reply-aware chunk construction. It must preserve the strongest relevant chunk while making room for diverse supporting context.

```text
Qdrant Stage 1 candidates
-> CrossEncoder rerank
-> reranker threshold gate
-> message-overlap dedupe
-> select top 5 deduped candidates
-> context sufficiency and token-budget gates
```

Dedupe remains n8n workflow logic. It does not require another runtime service.

## Current Readiness Assessment
The design has a sound core rule, but it is not fully implementation-ready on current `main`.

The agreed rule is:

```text
shared = intersection(chunk_a.message_ids, chunk_b.message_ids)
overlap_ratio = len(shared) / min(len(chunk_a.message_ids), len(chunk_b.message_ids))
```

If `overlap_ratio > 0.5`, keep the stronger candidate.

Targeted decisions and dependencies:

| Area | What is incomplete | Why it matters | Proposed resolution |
|---|---|---|---|
| Phase 5 dependency | Phase 5 reranking is now merged to `main` | Dedupe must know which overlapping chunk is stronger. Without the reranker and its scores, Phase 6 has no agreed basis for choosing which chunk to keep. | Done. Phase 6 extends the merged Phase 5 workflow. |
| Split-piece metadata | Current `main` copies the original chunk's complete `message_ids` onto every smaller piece created during token splitting. PR 5 corrects this. | Two pieces containing different text can appear to have 100% message overlap, causing dedupe to delete useful evidence. Existing Qdrant points remain wrong even after the code changes. | Merge PR 5 and rebuild Qdrant before validating dedupe. |
| Reply-root dedupe | Neither `main` nor PR 5 stores `root_message_id`. Issue 17 tracks this ingestion change. | Two chunks from the same Discord reply conversation may contain different child messages and therefore evade message-overlap dedupe. The LLM can still receive repeated conversation context. | Implement message-overlap dedupe in Phase 6. Add same-root handling after Issue 17 is completed and Qdrant is rebuilt. |
| Candidate scope | The contract says dedupe occurs after reranking but does not explicitly say whether to dedupe only the initial top five or every candidate that passed the reranker gate. | Deduping only five can leave fewer than five chunks with no replacements, even when candidates ranked 6-20 provide useful, distinct evidence. | Accepted: dedupe every reranker-passed candidate, then select the first five retained candidates. |
| Pairwise ordering | The overlap formula compares two chunks but does not define processing order, equal-score ties, or overlap chains such as A overlapping B and B overlapping C. | Different iteration orders could keep different chunks, making identical queries produce inconsistent context and tests. | Accepted: sort strongest-first, break ties by rerank order and Qdrant point ID, then compare each new candidate against chunks already kept. |
| Missing IDs | The contract does not define behavior when a Qdrant result has an empty or missing `message_ids` array. | The overlap denominator would be zero, and silently dropping the chunk could remove good evidence because of a metadata defect rather than low relevance. | Accepted: keep the candidate, mark it `not_comparable_missing_message_ids`, and expose the Qdrant point ID in observability. |
| Post-dedupe sufficiency | Observability defines a post-dedupe refusal category, but the retrieval contract does not explicitly state the minimum remaining evidence. | Retrieval and reranking could pass with three candidates, then dedupe could reduce them to one repeated conversation. Sending that to Gemini would violate the intended minimum-context rule. | Accepted: require at least three retained candidates; otherwise refuse before Gemini with `context_after_dedupe_insufficient`. |
| Reaction boost ordering | Some documentation says reaction boost occurs before dedupe; older wording places it afterward. The payload does not currently contain `reaction_count`. | If boosted scores decide which duplicate survives, ordering can change the retained evidence. Implementations could disagree even with the same inputs. | Accepted for Phase 6: skip reaction boost while the field is unavailable. Before adding it, decide whether dedupe compares raw `reranker_score` or `boosted_reranker_score`. |

This requires a short team review, not a redesign. The overlap formula and placement after reranking are solid.

## Proposed Deterministic Algorithm
Inputs are candidates that passed `reranker_score > 0`.

1. Sort by `reranker_score` descending.
2. Break equal-score ties by reranker rank, then stable Qdrant point ID.
3. Initialize `kept = []` and `dropped = []`.
4. Visit each candidate in sorted order.
5. If `message_ids` is empty, keep it and record `dedupe_status = not_comparable_missing_message_ids`.
6. Compare the candidate with every already-kept candidate that has message IDs.
7. Calculate the overlap formula using unique message-ID sets.
8. If any overlap ratio is greater than `0.5`, drop the current candidate because the stronger candidate is already kept.
9. Otherwise, keep the candidate.
10. Select the first five kept candidates for context assembly.
11. If fewer than three candidates remain, refuse before Gemini.

This greedy strongest-first algorithm is deterministic and naturally handles overlap chains without deleting the strongest evidence.

## Output Contract
Each reranked retrieval candidate should carry:

| Field | Meaning |
|---|---|
| `dedupe_status` | `kept`, `dropped`, or `not_comparable_missing_message_ids` |
| `dedupe_reason` | `no_overlap`, `message_overlap`, or `missing_message_ids` |
| `dedupe_matched_chunk_id` | Stronger chunk responsible for a drop |
| `dedupe_overlap_ratio` | Highest overlap ratio observed |
| `dedupe_shared_message_count` | Number of shared unique message IDs |
| `rank_after_dedupe` | Order among retained candidates |
| `selected_for_context` | Whether retained candidate entered final top five |

The workflow-level result should include:

- input candidate count
- kept candidate count
- dropped candidate count
- missing-message-ID count
- pairwise comparison count
- selected context count
- dedupe latency
- refusal reason, when applicable

## Observability
Phoenix should emit:

- `dedupe.started`
- `dedupe.chunk_dropped` for each removed candidate, or a bounded summary if event volume becomes noisy
- `dedupe.completed`
- `context.insufficient_after_dedupe` when fewer than three candidates remain

Postgres retrieval rows should persist `dedupe_status`, `dedupe_reason`, overlap evidence, and final selection state.

Phase 6 promotes the main dedupe outputs to first-class `rag_retrieval_results` columns because these fields are expected to be queried during debugging:

- `dedupe_status`
- `dedupe_reason`
- `dedupe_matched_chunk_id`
- `dedupe_overlap_ratio`
- `dedupe_shared_message_count`
- `rank_after_dedupe`
- `selected_for_context`

The payload JSON still keeps richer debug details such as `missing_message_ids_qdrant_point_id`.

Starting latency objective:

```text
p95 dedupe and context assembly < 150 ms
```

## Validation Cases
| Case | Expected behavior |
|---|---|
| No overlap | Preserve reranker order and keep all candidates |
| Exact duplicate message sets | Keep only the stronger candidate |
| Short chunk contained in a larger chunk | Drop the weaker chunk because the denominator uses the smaller set |
| Exactly 50% overlap | Keep both because the contract uses `> 0.5` |
| More than 50% overlap | Drop the weaker candidate |
| Transitive overlap chain | Deterministically preserve strongest-first diverse evidence |
| Equal reranker scores | Stable tie-break produces the same result on every run |
| Missing message IDs | Keep and flag the candidate; do not divide by zero |
| Fewer than three after dedupe | Refuse before Gemini with `context_after_dedupe_insufficient` |
| Split pieces | Compare only the message IDs actually present in each split piece |

## Implementation Sequence
1. Review and ratify the targeted decisions in this document.
2. Merge Phase 5 PR 13.
3. Merge the per-piece `message_ids` ingestion correction from PR 5.
4. Rebuild the Qdrant collection so stored payloads contain correct per-piece metadata.
5. Branch or rebase the Phase 6 workflow onto the merged Phase 5 implementation.
6. Add a pure n8n Code node after the reranker gate and before top-five context selection.
7. Persist candidate-level dedupe results and emit Phoenix dedupe spans.
8. Add n8n-friendly validation cases for all validation cases above. Prefer a workflow or reusable node path that can run regression questions through retrieval, rerank, and dedupe without requiring Gemini.
9. Run known duplicate-heavy queries and the regression question set.
10. Compare context diversity, refusal behavior, latency, and answer quality against Phase 5.

## Merge Gate
Phase 6 should not merge as complete until:

- Phase 5 reranking is present in its base branch.
- Qdrant has been rebuilt with correct per-piece `message_ids`.
- deterministic overlap fixtures pass
- Postgres records kept and dropped candidates
- Phoenix exposes dedupe evidence
- post-dedupe insufficiency refuses before Gemini
- known duplicate-heavy retrievals show less repeated context without losing the best answer

## Team Decisions Requested
1. **Which candidates are deduped?** Proposed: dedupe every candidate that passed the reranker gate, then choose the top five retained candidates. This lets a distinct candidate ranked below a duplicate replace the duplicate.
2. **How are overlapping candidates processed?** Proposed: strongest `reranker_score` first, then reranker rank and Qdrant point ID as stable tie-breakers. This makes repeated runs deterministic.
3. **What happens when `message_ids` is missing?** Proposed: keep and flag the candidate. This avoids discarding relevant evidence because of incomplete metadata while making the ingestion defect visible.
4. **What is the minimum context after dedupe?** Proposed: retain at least three candidates or refuse before Gemini with `context_after_dedupe_insufficient`. This preserves the existing minimum-evidence principle after duplicates are removed.
5. **Does reaction popularity affect which duplicate survives?** Proposed for Phase 6: no. Skip reaction boost until `reaction_count` exists; then separately approve whether dedupe compares raw or boosted reranker scores.

All five decisions were accepted in PR16 review. The implementation should include Haragonda's added observability note: when `message_ids` is missing, keep and flag the candidate and include the affected Qdrant point ID in the trace/payload.
