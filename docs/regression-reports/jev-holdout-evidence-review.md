# Jev holdout evidence review

This is an independent, manual review of the frozen 150-question Railway holdout. Both arms received the same live Qdrant candidates and ran through the test n8n workflow's production context assembly. The holdout was run after the Jev scoring rubric and `> 2.0` relevance gate were frozen. No threshold or prompt was adjusted using these results. Private questions, passages, and per-case judgments remain in ignored `logs/jev-experiment/` files.

All 150 paired retrieval runs completed. The raw retrieval decisions were: both answered 71, both refused 26, CrossEncoder answered and Jev refused 46, CrossEncoder refused and Jev answered 7. Jev reranking had a median of 174 ms and p95 of 270 ms in the holdout. The 117 versus 78 raw answer counts do not measure answer quality: the frozen sample includes statements and social exchanges as well as answer requests.

## Manual review population and method

An AI agent read all 150 original messages and both arms' complete *selected* context, labeling each message as a durable answer request, contextual or live-action request, or statement/other. It then judged each selected context as direct support (D), partial support or missing a key constraint (P), or no direct support (N). An echoed question received no answer credit. An anecdote about the wrong company, role, or time period did not satisfy a constrained request. This is a qualitative agent review, not a blinded human gold-label study.

| Frozen stratum | Durable answer requests | Contextual/live-action requests | Statements/other |
| --- | ---: | ---: | ---: |
| Active (30) | 16 | 9 | 5 |
| Passive, historically answered (60) | 9 | 18 | 33 |
| Passive, historically refused (60) | 4 | 13 | 43 |
| **Total (150)** | **29** | **40** | **81** |

For the 29 strict durable answer requests, the selected context contained a direct response in 13 CrossEncoder runs and 19 Jev runs. In 12 cases both selected direct support; in seven Jev selected direct support where CrossEncoder selected none or only partial support; in one CrossEncoder selected direct support while Jev selected none. Three cases gave both arms only partial support, and four gave neither arm direct support. Among these 29, the retrieval decisions were both answered 21, CrossEncoder refused/Jev answered 4, and CrossEncoder answered/Jev refused 4.

**These are selected-context support counts, not retrieval recall.** A refusal can be correct if no direct answer exists among the 20 Stage 1 candidates. Conversely, full-candidate inspection found a recent interview-experience request where a 2026 candidate contained useful process detail, but the selected Jev passage was a 2024 anecdote and the baseline selection echoed the request. The Jev final answer on a separate L8 role-scope request demonstrated that its selected context also contained a second, directly relevant 2026 passage; I corrected that case from partial to direct support after reading the complete selection. Full Stage 1 gold labeling across all 29 cases remains incomplete, so there is no defensible recall denominator or five-percentage-point retention verdict from this review.

I also inspected all 20 Stage 1 candidates for the eight durable requests where at least one arm refused. Jev's refusal missed direct rank-one evidence in one tailored job-choice case. Its refusals on the Guidewire, TPM side-work, and team-specific work-life-balance cases were supported by the absence of a direct candidate answer; CrossEncoder nevertheless answered two of those from insufficient evidence. CrossEncoder refused two cases where Jev selected direct Amazon compensation or ARM interview evidence. The latest Nvidia process and Reddit work-experience cases had only stale or partial candidate support, making the baseline refusals reasonable. This targeted adjudication is useful for failure diagnosis, but it does not complete gold labeling for all 29 cases.

The untouched holdout also exposed constraint failures: a “latest” company-news request selected stale material, a context-dependent follow-up selected an answer without the needed referent, and an event-recording request selected unrelated past events. A company culture request initially appeared unsupported but the selected context did contain directly relevant work-culture accounts, so it was not counted as a failure. These examples warrant review of final generated answers; downstream refusal may contain some retrieval risk. They also preclude a claim that Jev has passed the predeclared zero-new-unsupported-answer criterion at the retrieval stage.

The 40 contextual/live-action requests and 81 statements/other are retained as operational traffic, but they are not pooled into the 29-case durable evidence comparison. Their treatment requires separate product-policy judgment, especially where the bot should ask for missing context or avoid answering a conversational statement. These categories were assigned after the run and are exploratory; they must not be portrayed as a prespecified statistical subgroup.

## Decision and limits

The holdout establishes that the gateway and context assembly work on real Railway traffic and that Jev often provides useful evidence at lower model latency. It does **not** establish answer-quality equivalence or justify switching production. The final 20-case production Gemini answer comparison, including refusals, source support, and current-time constraints, is being reviewed separately. Switching requires the predeclared evidence-quality and latency criteria to hold on genuine answer requests and the total Railway-plus-Jev cost projection to be lower. The observed stale and context-mismatched retrieval selections need an explicit disposition before a switch.

**Test:** Re-read the ignored 150 paired results and original messages, including both arms' selected context; inspect full Stage 1 candidates before assigning false-refusal recall; then compare the real Gemini outputs for the diagnostic 20 cases.

**Pass when:** Every claimed denominator has a matching adjudicated candidate population, selected evidence directly supports the relevant company/role/time constraint, no new unsupported final answers appear, and the predeclared latency and cost gates pass. Otherwise retain the CrossEncoder production path and improve the Jev rubric on a new calibration set before another untouched holdout.
