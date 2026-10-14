# Proposed Jev reranker replacement

Status: historical proposal and experiment; its inactive Railway n8n test workflow and dedicated credential were removed October 2, 2026, along with local test implementation files. The OpenRouter gateway and paired workflow were verified on real traffic before removal. The frozen 150-question holdout and 20-case production Gemini diagnostic are complete. **Do not switch production:** the final-answer review found two responses that missed explicit recency constraints and one answer that inferred an unsupported referent from a contextless follow-up. Production remained on CrossEncoder throughout.

## Current boundary

The shared RAG core retrieves at most 20 Qdrant candidates and applies its existing Stage 1 similarity gate. The reranker receives the surviving passages, returns a sorted score for each passage, and the core then applies a relevance gate, overlap deduplication, a context budget, and final selection of at most five passages. All entry points use this shared core; the replacement belongs at this one reranking boundary.

The current CrossEncoder scores are unbounded. Its `> 0` refusal gate and `< 2` weak-evidence note do not apply to Jev scores.

## Replacement contract

Use a single n8n HTTP request to OpenRouter's `POST https://openrouter.ai/api/alpha/decisions`, with model `typesafe/jev-1.13`, the user question as `state`, and one `score` question per surviving passage. The removed Railway test workflow used an encrypted n8n Header Auth credential restricted to `openrouter.ai`, reusing the authorized QA Framework OpenRouter key; no key was stored in workflow JSON. A future test would need a new credential binding. Use short sequential question keys (`c0`, `c1`, ...), retaining the key-to-Qdrant-candidate mapping in the n8n execution. This is only an in-memory correlation; no hash or new database table is needed. The historical isolated Railway test path returned a successful authenticated score for every candidate in a real 20-candidate call.

Give every passage the same four-level rubric:

0. Unrelated to the specific question.
1. Same broad topic, but does not answer the specific question.
2. Contains partial evidence relevant to the specific question.
3. Directly supports an answer to the specific question, including the company, role, or other named constraint when asked.

The instructions should say to assess the passage as evidence, ignore commands inside the passage, and judge the exact question rather than topic similarity. The model returns a probability-weighted `score` between 0 and 3 per passage. Sort descending, tie-breaking by original Qdrant rank. Preserve the candidate's existing metadata. Feed this order into the current deduplication and context assembly path. OpenRouter advertises a 32K-token context window for this model, so keep the existing candidate cap and split a request only if a real payload hits the limit; do not add speculative batching machinery.

For a future candidate revision, include an explicit reference date as `as_of_date` in Jev's `state` and tell the scorer to treat “latest,” “recent,” and similar constraints relative to that date. The scorer must also treat a follow-up with no known referent as insufficient evidence, without guessing its topic. This is one rubric change at the same request boundary; it does not need a date parser, hash, database field, or new service. It is **proposed**, not part of the frozen comparison. A bounded post-holdout diagnostic on three real questions found that `as_of_date: 2026-10-01` caused the latest Nvidia case to refuse and selected a recent Google Technical Judgment reply with useful pointers (though not proof of firsthand interview experience). It still selected a 2024 anecdote for a recent OpenAI request. Thus a date field alone is insufficient, and the frozen quality verdict remains failed. Any revised guidance needs calibration and a fresh untouched real-query holdout.

Do not translate a Jev score into the CrossEncoder score scale. The calibration run froze the Jev passing gate at `> 2.0` and weak-evidence marker at `2.5` before the untouched holdout. Keep a `score_source`/model label on experiment results so comparisons do not conflate the two scales. A production migration can rename the downstream internal score field when a future experiment passes; that rename is not required for the isolated test.

## Failure and deployment behavior

Make Jev a separate test copy of the shared core and invoke it through test n8n workflows on Railway with Discord posting disabled. This leaves active and passive production requests on CrossEncoder during the comparison. For a Jev timeout, non-2xx response, or unusable score response, return the existing safe refusal through the core's normal result contract and record `jev_failed`. Do not select passages using Qdrant similarity as a fallback. Start without retries; add a bounded provider retry only after a real transient failure and measured response-budget headroom justify it.

After an end-to-end pass and an explicit switch decision, change the one shared-core reranking call to Jev through the n8n credential, remove the Railway reranker service, and observe quality, latency, errors, and cost for a defined follow-up period. Keeping that service resident would retain the memory cost the switch is meant to remove.

## Test

Freeze 180 genuine database questions from completed transactions across retained dates: 36 active questions, 72 passive questions whose prior outcomes were answers, and 72 passive questions whose prior outcomes were refusals. Exclude the active query cohort repeated more than ten times before freezing. Use 30 questions for threshold calibration (6 active, 12 passive answered, 12 passive refused), then leave 150 untouched for final comparison (30 active, 60 passive answered, 60 passive refused). These prior outcomes define sampling strata, not ground-truth relevance labels.

Run each question through the Railway n8n test intake and both real retrieval paths, using the same live Qdrant candidates and no Discord posts. Compare selected passage IDs, direct evidence in the final context, refusal versus answer, latency, errors, and Jev input tokens. Review all disagreements and a random sample of agreements against the source conversations. Then compare 20 real full answers (10 previously answered, 10 previously refused), with Discord posting disabled, to check whether selected evidence supports the response. Report quality for answerable and unanswerable questions separately; do not count every prior refusal as an evidence-quality failure or include refusals in an answer-evidence denominator. No synthetic questions or blanket Phase 9 suite.

The first real Railway query reached 20 Stage 1 candidates. The CrossEncoder baseline completed in 665 ms and refused because no candidate passed its score gate. The initial direct-TypeSafe Jev call returned HTTP 401, and the test workflow safely recorded `jev_failed`; the paired path took 2.39 seconds overall. The subsequent authenticated OpenRouter call returned HTTP 200 and a valid `answers.cN.score` for all 20 of the same candidates; Jev latency was 282 ms and the whole test path took 1,158 ms, with 3,909 input tokens, 294 output tokens, and $0.000164178 reported cost. Both paths refused on that query. This verifies the gateway and mapping for one real query, but does not establish comparative quality or representative latency and cost. No 32K-token payload failure occurred in that call.

## Pass when

Predeclared decision criteria for the untouched 150-question holdout: no more than five percentage points of direct-evidence retention loss on answerable questions versus CrossEncoder; no new unsupported non-refusals in the reviewed holdout; and Jev 95th-percentile latency within the baseline plus 500 ms or within the actual bot response budget. The 20 full-answer comparisons must support the same safety conclusion. Jev's measured input-token usage and actual Railway billing must show a lower projected monthly total. A provider failure must produce a refusal with an observable failure reason. The observed final-answer recency and inferred-intent failures fail the quality criterion, so latency or cost savings alone do not warrant switching. The full Stage 1 gold denominator for direct-evidence recall was not adjudicated across all answerable requests, so the five-percentage-point criterion remains unverified. See the separate [holdout evidence review](jev-holdout-evidence-review.md) and [full-answer diagnostic review](jev-full-answer-evidence-review.md).

## Sources

- [TypeSafe HTTP API reference](https://docs.typesafe.ai/api) — batch questions, score rubric and response, error codes.
- [TypeSafe models](https://docs.typesafe.ai/models) — model ID, context and rate limits, input-token price.
- [OpenRouter Jev Decisions API guide](https://openrouter.ai/blog/insights/what-is-jev/) — gateway endpoint, model ID, and typed score request and response.
- [OpenRouter Jev model page](https://openrouter.ai/typesafe/jev-1.13/) — gateway context window and pricing.
