# Jev full-answer diagnostic review

Twenty real holdout questions were deliberately chosen for a diagnostic end-to-end answer comparison on Railway. Each arm used the same live Stage 1 candidates, its own reranked and assembled context, and the production Gemini request and result nodes. Discord posting was disabled. This was a risk-focused sample, not a random sample or a new holdout; its percentages must not be generalized to all traffic. The questions and full source/answer text are retained only in ignored `logs/jev-experiment/full-all-results.jsonl`.

The CrossEncoder path produced 10 answers, 8 refusals, and 2 generation failures. The Jev path produced 18 answers, no refusals, and 2 generation failures. All Gemini calls that were made returned HTTP 200. The four generation failures (two per arm) were `MAX_TOKENS`/output-integrity failures: model thoughts consumed nearly the 5,000 output-token budget and the response JSON was truncated. They are failures of the existing Gemini generation budget, not evidence that one reranker hallucinated. No retry or configuration change was made during the frozen test.

## Manual answer judgment

An AI agent read all 20 original requests, both final responses, and the relevant selected passages. It checked whether each answer addressed the actual request, whether its substantive claims were traceable to selected source text, and whether cited dates, companies, and roles met any explicit constraint. A citation marker alone did not count as support. The agent's qualitative Jev judgments across the 20 cases are:

| Final-answer judgment | Cases | Meaning |
| --- | ---: | --- |
| Supported response | 14 | The selected community passages support the material answer to the request. |
| Partial response | 1 | The passage supports a narrow facet, but does not cover the broader workplace question. |
| Time-constraint miss | 2 | The answer accurately identifies old source material but does not satisfy a request for recent or latest experience. |
| Unsupported inferred intent | 1 | A contextless follow-up is treated as a particular topic without evidence of the intended referent. |
| Generation failure, not judgeable as an answer | 2 | Gemini output was truncated before a valid response could be returned. |

The two time-constraint misses are historical interview accounts used for requests explicitly asking for latest Nvidia and recent OpenAI experiences. Both responses disclose their source age; the issue is responsiveness to the time constraint, not fabricated source facts. The unsupported-intent case is an ambiguous Nvidia follow-up answered as if it asked about job openings; the baseline path refused for lack of context. Those three Jev answers break the predeclared requirement of no new unsupported or constraint-mismatched non-refusals in reviewed real requests. A separate Reddit workplace answer is only a compensation/leveling anecdote and remains partial rather than being credited as a broad work-culture answer.

The 14 supported Jev answers include useful direct evidence that the baseline refused in some cases, such as company culture, interview process, and senior role scope. That benefit does not erase the above intent and recency failures. The baseline's 10 generated answers comprised nine supported answers and one weak, mostly redirective response to a recent interview request. This diagnostic sample is too small and purposefully selected to estimate a population quality rate or to conclude general superiority for either arm.

This risk-focused full-answer diagnostic drawn from held-out questions therefore does **not** justify a production switch despite successful Jev calls and lower reranker latency. Keep the CrossEncoder path in production. If Jev is pursued, revise the general scoring guidance for time constraints and missing conversational referents, calibrate on real questions, then use a fresh untouched real-query holdout with the same final-answer review; do not tune against these 20 cases and call them a new holdout. Separately, the Gemini output budget failures should be investigated at the generation component and retested on the affected real journeys.

After the frozen run, a separate three-question diagnostic added an October 1, 2026 reference date to Jev's decision state without changing its score gate. This made the latest Nvidia request refuse, selected a relevant August 2026 reply with Google Technical Judgment pointers (but no proof of the respondent's firsthand experience), and still selected a 2024 anecdote for the recent OpenAI request. These are observations about a possible fix, not revised holdout results or a basis for switching.

**Test:** Replay the 20 retained real questions through the Railway test n8n workflow with Discord disabled; compare each output with the original request and complete selected source passages, marking generation failures separately from answer quality.

**Pass when:** Both arms complete without generation truncation, and Jev introduces no unsupported inferred-intent or explicit-time-constraint failures while retaining direct evidence on the prespecified real-query holdout. The current run fails this quality gate.
