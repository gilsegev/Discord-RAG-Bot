# TPM Unite RAG Bot — Evaluation & Feedback Scoring Design

**Owner:** (shilpigpt) · **Status:** Draft for review · **Related:** Product Vision & Requirements; Gil's arch overview

## Purpose
The v1 bot was deprecated for generic, ungrounded advice. Evaluation's job is to prove answers are relevant and grounded in TPM Unite's own history — before launch and continuously after — and to catch that failure before the community sees it.

## What we measure
Each answer is scored 0–2 against a shared rubric on five dimensions: **retrieval quality** (did we fetch the right community context), **groundedness** (every claim supported by that context, not invented — the v1 killer), **answer relevance**, **appropriate refusal** (declines with the specified message when context is insufficient), and **framing/tone** (community wisdom, not authoritative rulings, per the system-prompt clause). PII/safety violations are flagged. *(Detailed scoring rubric to follow as an appendix.)*

## Two evaluation tracks
- **Offline (curated):** ~40–60 real questions with known expected behavior — happy-path, nuanced/subjective, no-context (refusal), and adversarial/PII cases. Used to gate launch and to catch regressions by re-running the same set on every prompt, retrieval, or model change. Stored as a dataset in the observability backend (such as Langfuse/Phoenix) and annotated in-tool — no spreadsheet.
- **Online (production):** the implicit 👍/👎 and explicit critique that Gil's feedback-correlation layer ties back to each transaction and grades automatically. Every 👎'd question becomes a new curated case — this is how the set grows over time.

These are complementary, not redundant: production feedback can't grade a bot before launch, and a 👍 measures satisfaction, not groundedness. The curated set covers both gaps.

## Metrics
Quality side of the weekly **#bot-metrics** digest: context-found rate, groundedness pass rate, correct-refusal rate, thumbs-up %. Operational metrics (latency, volume) stay with Observability.

## Proposed launch gates (team to ratify)
Groundedness ≥ 90%; **zero** ungrounded answers on no-context questions (non-negotiable — the v1 failure); correct-refusal ≥ 85%; framing/tone ≥ 90% on nuanced cases.

## Phased plan
| Phase | What | Code |
|---|---|---|
| 0 | Rubric + ~40-case curated set, annotated in Langfuse/Phoenix | None (UI) |
| 1 | Quality slice of the weekly digest from feedback data | Light |
| 2 | LLM-as-judge auto-scoring + regression runs on every change | More |

## Open questions for the team
1. The explicit feedback is free-text critique — do we want a structured "why" menu so failure reasons are machine-categorizable?
2. Finalize launch-gate numbers (above).

---

## Appendix A — Detailed scoring rubric

This backs the five-dimension rubric named in *What we measure*. 

**How to score one case.** For each case the annotator needs three things on screen — the user's question, the context the bot retrieved, and the bot's final answer — and scores each dimension independently 0–2. (This is *why* the inference logs must capture retrieved context: without it, groundedness cannot be scored.)

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| **Retrieval quality** | Retrieved context is largely irrelevant to the question | Partially relevant — useful chunks mixed with noise, or the key context ranked too low to be used | The most relevant available community context was retrieved and usable |
| **Groundedness** *(highest priority)* | One or more claims are unsupported by the retrieved context — invented detail or injected generic advice (the v1 failure) | All major claims supported; minor embellishment or a detail that slightly overreaches the context | Every claim in the answer is directly traceable to the retrieved context |
| **Answer relevance** | Doesn't address the question | Partially addresses it, answers a near-miss question, or is incomplete | Directly and usefully answers what was asked |
| **Appropriate refusal** | Answered when it lacked sufficient context, **or** refused when good context existed | Right decision, wrong execution — e.g. correctly refused but didn't use the specified message, or answered with needless hedging | Right decision and execution: answered on sufficient context, or refused using the exact specified message |
| **Framing / tone** | Authoritative/prescriptive, positions itself as a replacement for human discussion, or omits the required caveat on a nuanced topic | Appropriate framing but missing the closing caveat where it was warranted, or slightly too authoritative | Frames as community wisdom and includes the caveat on nuanced/subjective/evolving topics |

**Reference strings the rubric checks against:**
- *Exact refusal message* (from the requirements doc): "I don't have enough TPM Unite specific context to answer this confidently, try rephrasing or ask the community directly."
- *Required caveat* (from the system-prompt clause): nuanced/subjective/evolving answers should close by noting the response reflects past TPM Unite discussions and that the community may have more recent or personal context to add.

**How 0–2 maps to the launch gates.** A dimension "passes" a case at **score = 2** (strict — the team can revisit). That makes the body's gates computable:
- *Groundedness pass rate* = % of cases scoring 2 on groundedness → gate ≥ 90%
- *Correct-refusal rate* = % of refusal-category cases scoring 2 → gate ≥ 85%
- *Framing/tone* = % of nuanced cases scoring 2 → gate ≥ 90%
- *Non-negotiable gate* = **zero** no-context cases scoring 0 on groundedness (the bot must never fabricate an answer when it had no real context)

**PII / safety flag.** Separate from the 0–2 scales: any answer that surfaces personal/identifying information or other harmful content is flagged as a blocker and fails the case outright, regardless of its other scores. Flag criteria to be defined with the Privacy/Safety workstream.

**Worked example** (illustrative):
- **Channel:** #interview-prep · **Question:** "How should I prep for the Amazon TPM loop?"
- **Retrieved context:** two past threads on Amazon's leadership-principles-heavy behavioral rounds and the bar-raiser interviewer; one notes system-design depth varies by team.
- **Bot answer:** summarizes the leadership-principles and bar-raiser points, then adds "and grind 200 LeetCode problems first."
- **Scores:** retrieval **2**, groundedness **0** (the LeetCode claim appears nowhere in the retrieved context — injected generic advice), relevance **2**, refusal **2**, framing **1** (no caveat).
- **Verdict:** fails on groundedness despite reading as helpful. This is exactly the v1 failure the rubric exists to catch — the fix isn't a tone tweak, it's stopping the model from adding claims the context doesn't support.

**Calibration.** Before trusting the rubric, two people independently score the same ~10 cases. Large disagreement means a level is underspecified — tighten the wording before scaling.
