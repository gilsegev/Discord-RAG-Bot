# TPM Unite RAG Bot — Evaluation & Feedback Scoring Design

**Owner:** (shilpigpt) · **Status:** Reviewed — decisions agreed · **Related:** Product Vision & Requirements; Arch Overview; Observability Design; Feedback & Reaction Correlation Design

## Purpose
The v1 bot was deprecated for generic, ungrounded advice. Evaluation's job is to prove answers are relevant and grounded in TPM Unite's own history — before launch and continuously after — and to catch that failure before the community sees it.

## Ownership & interfaces
This workstream owns the regression question set, eval labeling/scoring, and feedback/reaction correlation (the correlation mechanism is specified in its own design doc, not here). Observability (Gil) owns the trace infra, Postgres/Phoenix wiring, and the weekly rollup/digest. Regression runs produce durable run/result evidence first. Human or judge scoring then writes labels to the app-owned Postgres table `rag_eval_labels` (`transaction_id`, `dimension`, `label`, `failure_type`, `source`, `labeler`, `created_at`); the weekly `rag_weekly_metrics` rollup reads from it. Phoenix holds annotations and review context, not the source of truth.

## What we measure
Manual grading (Phases 0–1) is a strict **binary Pass/Fail** per dimension — humans are far more consistent on a yes/no than on a graded scale, which keeps weekly grading fast and the data clean. Three dimensions are graded by hand: **groundedness** (every claim supported by the retrieved context, not invented — the v1 killer), **answer relevance** (does the reply actually address the question asked — judged from the question + answer alone), and **tone/refusal** (frames as community wisdom not authoritative rulings, and declines with the specified message when context is insufficient). **Retrieval quality is not hand-graded** — a human can judge whether the retrieved context was *sufficient* to answer, but not whether it was the *best available* without searching the whole corpus; it is inferred indirectly (high groundedness + relevance imply retrieval succeeded) and tracked at the aggregate level via context-found rate, with recall@k added in Phase 2. PII/safety violations are an outright fail. Granular (0–2 / decimal) scoring is reserved for Phase 2, where the LLM-as-judge computes it without human fatigue. *(Detailed rubric in the appendix.)*

## Two evaluation tracks
- **Offline (curated):** ~40–60 real questions with known expected behavior — happy-path, nuanced/subjective, no-context (refusal), and adversarial/PII cases. Used to gate launch and to catch regressions by re-running the same set on every prompt, retrieval, or model change. Questions are mined from real community history (approved); near-duplicates of indexed content are held out or marked so the set isn't just testing trivial retrieval. Runs write regression evidence first; labels are written to `rag_eval_labels` after human or judge scoring. Phoenix holds review context. No spreadsheet.
- **Online (production):** the implicit 👍/👎 and explicit critiques members leave on live answers. These are a *satisfaction signal and a review trigger* — **not** eval labels: a 👎 flags the transaction for human review and becomes a new curated case, which is how the set grows. How feedback is captured and tied to a transaction is specified in the Feedback & Reaction Correlation design, not here.

These are complementary, not redundant: production feedback can't grade a bot before launch, and a 👍 measures satisfaction, not groundedness. The curated set covers both gaps.

## Metrics
Quality side of the weekly **#bot-metrics** digest (rolled up by Observability from `rag_eval_labels`): context-found rate, groundedness pass rate, correct-refusal rate, thumbs-up %, and a single **RAG Reliability Index (RRI)** — a weighted composite of the critical gates that gives one comparable trend line across weeks of varying traffic (it's a rate, so already traffic-normalized). Formula: **RRI = 0.7 × groundedness pass rate + 0.3 × correct-refusal rate**. The component rates are always shown *alongside* RRI — a single number can hide a groundedness drop offset by a refusal rise — as is the week's sample size (n), since low-traffic weeks make the percentage swing. The hard no-context gate (below) stays *separate* from RRI: it's a pass/fail floor, not part of the average. Operational metrics (latency, volume) stay with Observability.

## Launch gates (ratified)
Groundedness ≥ 90%; **zero** ungrounded answers on no-context questions (non-negotiable — the v1 failure); correct-refusal ≥ 85%; framing/tone ≥ 90% on nuanced cases.

## Phased plan
| Phase | What | Code |
|---|---|---|
| 0 | Binary Pass/Fail rubric + ~40-case curated regression set; manual labels to `rag_eval_labels`, review in Phoenix | None (UI/SQL) |
| 1 | Production feedback wired in (per the correlation design); 👎s feed the review queue, labels flow to the digest | Light |
| 2 | LLM-as-judge granular scoring (Gemini, validated against human labels first) + regression runs on every change through the shared RAG core | More |

## Decisions (agreed with Gil)
- **Relevance kept** as a third manual dimension — *answer* relevance, judged from question + answer (distinct from retrieval quality, which is dropped).
- **Refusal vocabulary:** `correct_refusal`, `false_refusal`, `missed_refusal`, `no_context_violation`. Stored in `rag_eval_labels.failure_type`.
- **Gates and RRI weights ratified** (above). Weights are hardcoded in both this doc and `rag_weekly_metrics.rag_reliability_index` — change them together.
- **Judge:** Gemini, validated against human labels before reliance; Gil's API key for now.
- **Calibration:** this workstream delivers the labeled set; Hemanth recalibrates reranker thresholds against it.
- **Adjudication of expected-behavior labels:** crowdsource in Discord or review with Gil.
- **Feedback capture & correlation:** specified in the Feedback & Reaction Correlation design — which also resolves the earlier "structured why-menu vs free-text" question (v1 uses both).

---

## Appendix A — Detailed scoring rubric

This backs the rubric named in *What we measure*. It sits **outside** the one-page main doc on purpose: the main doc states the approach; this is the working reference annotators use when scoring.

**How to score one case.** For each case the annotator needs three things on screen — the user's question, the context the bot retrieved, and the bot's final answer — and marks each dimension **Pass or Fail**. (This is *why* the inference logs must capture retrieved context: without it, groundedness cannot be judged.) Each mark is written to `rag_eval_labels` as one row (`dimension`, `label`, `failure_type`, `source`, `labeler`). Retrieval quality is not graded here — see *What we measure* for why it's inferred and tracked at the aggregate level instead.

| Dimension | Pass | Fail |
|---|---|---|
| **Groundedness** *(highest priority)* | Every claim is directly traceable to the retrieved context | Any claim is unsupported by the context — invented detail or injected generic advice (the v1 failure) |
| **Answer relevance** | Directly and usefully addresses the question asked | Doesn't address it, answers a near-miss question, or is materially incomplete |
| **Tone / Refusal** | Followed the rules: answered on sufficient context **or** refused using the exact specified message, and framed as community wisdom with the caveat where required | Broke a rule: answered when it lacked context, refused without the specified message, was authoritative/prescriptive, or omitted a required caveat |

**Reference strings the rubric checks against:**
- *Exact refusal message* (from the requirements doc): "I don't have enough TPM Unite specific context to answer this confidently, try rephrasing or ask the community directly."
- *Required caveat* (from the system-prompt clause): nuanced/subjective/evolving answers should close by noting the response reflects past TPM Unite discussions and that the community may have more recent or personal context to add.

**How Pass/Fail maps to the gates.** A "pass rate" is simply the % of cases marked Pass on that dimension. That makes the body's gates computable:
- *Groundedness pass rate* = % Pass on groundedness → gate ≥ 90%
- *Correct-refusal rate* = % of refusal-category cases that Pass on Tone/Refusal → gate ≥ 85%
- *Tone/framing* = % Pass on nuanced cases → gate ≥ 90%
- *Non-negotiable gate* = **zero** no-context cases that Fail groundedness by fabricating an answer (logged as `no_context_violation`)
- *RRI* = 0.7 × groundedness pass rate + 0.3 × correct-refusal rate, reported with its components and sample size (see Metrics)

**Refusal outcomes** (stored in `rag_eval_labels.failure_type` on the Tone/Refusal dimension):
- `correct_refusal` — refused correctly on weak/no context (the Pass outcome)
- `false_refusal` — refused despite usable context (Fail)
- `missed_refusal` — answered when it should have refused (Fail)
- `no_context_violation` — critical subset of missed refusal where no usable context existed (Fail; trips the non-negotiable gate)

**PII / safety flag.** Separate from the Pass/Fail dimensions: any answer that surfaces personal/identifying information or other harmful content is flagged as a blocker and fails the case outright, regardless of its other marks. Flag criteria to be defined with the Privacy/Safety workstream.

**Worked example** (illustrative):
- **Channel:** #interview-prep · **Question:** "How should I prep for the Amazon TPM loop?"
- **Retrieved context:** two past threads on Amazon's leadership-principles-heavy behavioral rounds and the bar-raiser interviewer; one notes system-design depth varies by team.
- **Bot answer:** summarizes the leadership-principles and bar-raiser points, then adds "and grind 200 LeetCode problems first."
- **Marks:** groundedness **Fail** (the LeetCode claim appears nowhere in the retrieved context — injected generic advice); relevance **Pass**; tone/refusal **Pass**.
- **Verdict:** the case fails on groundedness despite reading as helpful — and any groundedness Fail fails the case. This is exactly the v1 failure the rubric exists to catch: the fix isn't a tone tweak, it's stopping the model from adding claims the context doesn't support. Note retrieval itself was fine (the right threads came back) — proof that good retrieval doesn't guarantee a grounded answer, which is why groundedness is graded directly and retrieval only inferred.

**Calibration.** Before trusting the rubric, two people independently grade the same ~10 cases. Frequent disagreement means a Pass/Fail boundary is underspecified — tighten the wording before scaling. (Binary marks make this far easier to reach agreement on than a graded scale, which is the point of the switch.)
