# Phase 6: Post-Rerank Dedupe
**Status:** Design review and implementation preparation
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

Targeted decisions and dependencies still need closure:

| Area | Current state | Recommendation |
|---|---|---|
| Phase 5 dependency | Phase 5 reranking is still PR 13, not in `main` | Merge Phase 5 before integrating the Phase 6 workflow |
| Split-piece metadata | Current `main` leaves the original full `message_ids` on every split piece | Merge PR 5's per-piece `message_ids` fix and rebuild Qdrant before validating dedupe |
| Reply-root dedupe | `root_message_id` is not in the payload | Implement message-overlap dedupe now; retain reply-root dedupe as a documented extension |
| Candidate scope | The contract does not state this as an executable algorithm | Apply dedupe to every candidate that passes the reranker threshold, then select the top five |
| Pairwise ordering | The formula does not define deterministic transitive handling or ties | Sort strongest-first and use deterministic greedy selection |
| Missing IDs | Empty or missing `message_ids` behavior is unspecified | Keep the candidate, mark dedupe as not comparable, and emit an observability warning |
| Post-dedupe sufficiency | Observability names the refusal but the retrieval contract is not explicit | Refuse when fewer than three deduped candidates remain, using `context_after_dedupe_insufficient` |
| Reaction boost ordering | Current docs have conflicting historical wording | Skip the boost while `reaction_count` is unavailable; separately ratify whether dedupe strength uses raw or boosted reranker score |

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
8. Add deterministic unit-style fixtures for all validation cases above.
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
1. Confirm dedupe runs across all reranker-passed candidates before selecting top five.
2. Confirm strongest-first greedy matching and the proposed tie-break order.
3. Confirm missing-message-ID candidates are retained and flagged.
4. Confirm `context_after_dedupe_insufficient` when fewer than three candidates remain.
5. Confirm Phase 6 skips reaction boost until `reaction_count` exists, or explicitly define boosted-score ordering now.
