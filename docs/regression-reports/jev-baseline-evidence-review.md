# Provisional evidence review of the current reranker

This is a bounded manual review of actual Railway baseline results, not a Jev comparison or an estimate of whole-cohort quality. The reviewer inspected the full selected context for the first five completed results available in each frozen sampling stratum (15 total: five active, five previously passive answered, five previously passive refused). This is an availability sample, not a random or held-out estimate. Per-case identifiers and judgments are retained only in ignored `logs/jev-experiment/baseline-evidence-review.json`; this report omits the Discord message text.

| Observed category | Count in 15 | Reading |
|---|---:|---|
| Statements without an answer-seeking request | 6 | Useful for measuring what gets selected, but cannot be scored as question-answer evidence. |
| Questions or requests | 9 | Includes community, social, and time-sensitive requests; not all are useful for durable RAG answers. |
| Questions/requests with directly supporting selected replies | 3 | One interview-preparation request and two historical logistics/referral requests. The latter two replies are tied to past dates and cannot safely establish current availability. |
| Question with partial or uncertain selected evidence | 1 | A layoff discussion contains hearsay and related material but does not independently settle the claim. |
| Question with an unsupported non-refusal | 1 | A company-specific post-interview wait-time question has one selected passage that repeats the question without providing a wait time. |
| Questions/requests with no selected passage | 4 | Refusals or absent evidence; selected-output review alone cannot prove whether the corpus contained a missed answer. |

Historical replay creates a concrete evidence trap: the current Qdrant corpus can return the original question or statement. An exact or near-exact text match may receive a high reranker score even if the passage offers no answer. Conversely, a chunk that echoes a question and contains a genuine reply can still support an answer. Reviewers must judge the reply content, not the echoed question, and should track this condition for both models. This matters most for the active wait-time example, where the current baseline advanced toward an answer from an echo alone.

The frozen cohort's prior `answered`/`refused` status is a sampling label, not a relevance label. A separate answer-seeking subgroup and a separate treatment of current-action requests are necessary for the final holdout decision. Count direct evidence only when selected passages contain facts responsive to the actual request and its named constraints. Inspect all selected passages for that judgment, and review disagreements plus sampled agreements. Do not convert the 15-case observations into a quality rate for the 180-question cohort.

## Test

After an authenticated Jev run, apply the same direct-support and temporal criteria to both arms on the frozen holdout, with identical Stage 1 candidates. Inspect all selected passages, including cases where the original question is embedded in a chunk, and compare a small real full-answer set with Discord posting disabled.

## Pass when

The Jev recommendation is based on genuine answer-seeking cases and reviewed source support, with echoed questions receiving no credit by themselves. Current-model observations here remain provisional until the live Jev outputs exist; no Jev quality conclusion follows from this review.
