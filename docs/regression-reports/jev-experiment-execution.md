# Jev replacement experiment execution

Status: historical experiment completed on Railway production infrastructure. On October 2, 2026, the inactive Jev test workflow, its dedicated n8n OpenRouter credential, and the local test workflow and runner were removed as part of the implementation rollback. The ignored case-level results remain for audit. **Do not switch production reranking.** The active shared core remains on CrossEncoder.

The test webhook performed one live query embedding and Qdrant search, then sent the same Stage 1 candidates to the local CrossEncoder and OpenRouter Jev (`typesafe/jev-1.13`). Each arm passed through the shared core's existing deduplication and context-budget assembly code. The Jev gate was frozen at score `> 2.0` with a weak-signal note below `2.5` after 30 calibration cases and before any of the 150 holdout cases. No Discord message was posted or evaluation label written. The OpenRouter key was reused from the authorized QA Framework project and stored as an encrypted n8n HTTP-header credential; the workflow file contains only a credential reference. This test covers live retrieval through final Gemini answers; it does not exercise production intake routing, the pre-retrieval safety gate, Discord posting, or transaction persistence.

The frozen sample comprised 180 distinct questions from completed historical production database transactions: 36 low-repeat active calls, 72 historically answered passive candidates, and 72 historically refused passive candidates. Thirty cases were used for calibration (6/12/12); the remaining 150 were untouched holdout cases (30/60/60). Forty-eight active questions repeated more than ten times each in the recent DB period and were excluded from this sample because they appeared replay-like. The sample questions and case-level outputs remain in ignored `logs/jev-experiment/` files, not in Git. The historical outcome is a sampling stratum, not an answer-quality gold label. Some passive messages are statements or live-action requests, which the manual review separates from durable answer requests.

| Live test | Completed | Main observation |
| --- | ---: | --- |
| Final calibration | 30/30 | Used to freeze `> 2.0` Jev gate and generic exact company/role/time scoring guidance. |
| Untouched paired holdout | 150/150 | All Jev calls returned HTTP 200 and usable scores; 53 answer/refusal decisions differed. |
| Diagnostic full Gemini answers | 20/20 | Baseline: 10 answers, 8 refusals, 2 generation failures. Jev: 18 answers, 0 refusals, 2 generation failures. |
| Separate as-of-date diagnostic | 3/3 | Latest Nvidia changed to refusal; Google recency selected useful recent but not firsthand evidence; recent OpenAI still selected a 2024 account. |

In the 150-case holdout, baseline retrieval answered 117 and Jev retrieval answered 78. Jev's median/p95 reranker latency was **174/270 ms** versus CrossEncoder's **2,274/3,143 ms**. OpenRouter reported **933,563 input tokens** and **$0.039209646** total Jev inference cost, or **$0.00026139764 per call**. The first-attempt failure count was zero in this frozen holdout. An earlier baseline-only run initially received HTTP 422 for three non-ASCII question bodies because the Windows PowerShell 5 client did not send explicit UTF-8 bytes; fixing the runner's encoding recovered all three. That earlier baseline-only run preceded final context-budget assembly and is not used as the final quality comparison.

The independent holdout evidence review judged all 150 original messages and both selected contexts, including all 29 strict durable answer requests. Direct selected support appeared in 19 Jev and 13 CrossEncoder cases among those 29, but full Stage 1 gold labeling was incomplete, so these are **not retrieval recall rates**. The 20-case full-answer sample was intentionally risk-focused, not representative. Source review found 14 supported Jev answers, one partial answer, two explicit recency-constraint misses, one unsupported inferred-intent answer, and two Gemini generation failures. The Nvidia “latest” and contextless Nvidia follow-up answers were new Jev failures relative to baseline refusals. The recent OpenAI request was also answered from old material; baseline's answer was weak. These results violate the predeclared requirement of no new unsupported or constraint-mismatched non-refusals. See [holdout evidence review](jev-holdout-evidence-review.md) and [full-answer evidence review](jev-full-answer-evidence-review.md) for case-level reasoning.

The four Gemini failures across both arms were HTTP 200 responses with `MAX_TOKENS`: model thoughts consumed nearly the production 5,000 output-token limit, leaving truncated JSON. The existing output-integrity guard safely returned failure. They are generation-component failures, not Jev API errors, and no retry or budget change was made during the frozen experiment.

The historical test implementation used `workflows/n8n/jev-reranker-comparison-test.json` and `scripts/run-jev-reranker-comparison.ps1`. Both files, the inactive remote workflow, and its dedicated encrypted credential were removed on October 2, 2026. The retained private sample and results document the completed run, but the test is no longer runnable as-is. A future authorized Jev experiment would require a new isolated workflow, credential binding, runner, and fresh untouched real-query holdout.

**Decision:** Keep CrossEncoder in production. OpenRouter Jev was operationally reliable and much faster in this run, and [actual Railway billing analysis](jev-railway-cost-analysis.md) projects savings only if the resident reranker is retired. The answer-quality gate failed. A next attempt should address missing conversational referents and time constraints in the general decision contract, independently fix Gemini output-budget truncation, then calibrate and evaluate against a *new* untouched real-query holdout with final-answer review. The three as-of-date runs are exploratory and do not change this verdict.

**Test for any future proposal:** Build a new isolated Railway n8n test workflow, replay distinct real database questions, inspect full Stage 1 and selected evidence for every genuine answer request, and run real Gemini answer cases with Discord disabled. Verify provider status, refusal, source support, latency, and billed tokens.

**Pass when:** The new untouched holdout has no additional unsupported or constraint-mismatched Jev answers, direct evidence remains available for genuine answer requests, latency stays within the bot budget, and Railway savings exceed measured Jev charges after the local service is retired. This experiment failed the answer-quality condition.

## Implementation self-review

I reviewed the delivered test workflow, runner, credential reference, and report against the requested narrow experiment. The copied production context code retains its existing prompt-hash calculation for its normal internal contract; the Jev candidate mapping uses simple sequential `c0`, `c1`, ... keys and adds no hash-based correlation. No production workflow or reranker service was changed. The encrypted credential is referenced by ID only, private case text/results are Git-ignored, and the test webhook is deactivated after the run. A structural check found all workflow connections valid; the real Railway runs above verified the active branches, safe Jev failure, Gemini skip path, and UTF-8 client transport. The broad Phase 9 suite and synthetic journeys were not run.

Two implementation defects were found and fixed during execution: Windows PowerShell 5 encoded three non-ASCII JSON requests incorrectly, and the runner now sends explicit UTF-8 bytes; the runner's default Jev threshold was `1.5` after the frozen gate became `2.0`, and its default now matches the frozen gate. The copied reranker decision initially labeled Jev results as CrossEncoder in internal metadata; the Jev response adapter now names the Jev model. A separate, explicit scope deviation remains: the isolated test starts after production intake and pre-retrieval safety, so it cannot qualify those gates or the Discord posting path. This was chosen to keep the same live candidate set in both arms and avoid production side effects; it requires a real production-path safety check before any future switch. No other fixable deviation was found in this bounded implementation review. No additional review agents were used because the user assigned each workstream to one standalone agent.

```text
Real DB question -> Railway test webhook
                 -> embed -> live Qdrant -> Stage 1
                         | same candidates |
                         +-> CrossEncoder -> dedupe/context -+
                         +-> OpenRouter Jev -> dedupe/context +-> compare
                                                 |
                                      selected 20 -> Gemini per arm
                                                 |
                                      private results; no Discord post
```
