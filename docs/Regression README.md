# Regression Question Set

Curated evaluation data for the Evaluation and Feedback Scoring workstream. The full rubric, gates, metrics, and launch criteria live in [evaluation-and-feedback-scoring-design.md](evaluation-and-feedback-scoring-design.md). This README describes the canonical question file used by the Phase 8 regression harness.

## Why This Exists

The v1 bot was deprecated for ungrounded, generic answers. This set is the fixed yardstick that proves the new bot is grounded and refuses appropriately.

Use it as:

- the launch gate before passive listener behavior
- the regression check after prompt, retrieval, rerank, dedupe, model, schema, or corpus changes
- the calibration set for reranker thresholds
- the seed set for later LLM-as-judge scoring

## Contents

| File | Role |
|---|---|
| [regression_questions.jsonl](../scripts/regression_questions.jsonl) | Canonical set; one eval case per line |
| `export_review_csv.py` | Future helper for Sheets-friendly adjudication output |
| [Regression README.md](Regression%20README.md) | This file |

## Case Schema

| Field | Meaning |
|---|---|
| `id` | Stable case ID, such as `RQ-001`. Never reuse IDs |
| `category` | `happy_path`, `nuanced_subjective`, `personal_context`, `no_context_refusal`, or `adversarial_pii` |
| `channel_scope` | `all`, or a channel-scoped instruction such as `in #tpm-tradecraft`. Scoped cases intentionally constrain retrieval to that channel for evaluation; ordinary bot questions should still search across the indexed corpus unless explicitly scoped |
| `question` | Prompt sent to the bot or retrieval harness |
| `expected_action` | `answer` or `refuse` |
| `expected_caveat` | `yes` when a nuance, temporal, or personal caveat is required |
| `expected_flags` | `pii_block` when the answer must not surface PII, otherwise `none` |
| `contamination_risk` | `review` when the question is close to a single indexed thread, otherwise `low` |
| `source_area` | Corpus area the case relates to; may include `PROVISIONAL` notes |
| `expected_behavior` | Human-readable behavior label; not a frozen answer |

## Current Set

| Category | Count | Expected Action |
|---|---:|---|
| `happy_path` | 16 | answer |
| `nuanced_subjective` | 8 | answer with caveat |
| `personal_context` | 5 | answer as community wisdom with caveat |
| `no_context_refusal` | 10 | refuse |
| `adversarial_pii` | 6 | refuse or block PII |

Totals:

- 45 cases
- 29 expected answers
- 16 expected refusals
- 13 caveat-required cases
- 6 PII/adversarial cases

## How It Maps To The Rubric

Each case carries expected behavior, not a gold answer.

Regression execution first writes durable evidence to `rag_regression_runs` and `rag_regression_results`.

When label writing is explicitly enabled, the regression harness can also write derived labels to `rag_eval_labels` with `source = regression`. These labels are useful for dashboards and trend checks, but they should be treated as automated regression labels, not human adjudication.

- `expected_action` maps to the Tone/Refusal dimension.
- `expected_action = answer` requires Groundedness and Answer Relevance to pass.
- `expected_caveat = yes` requires the nuance, temporal, or personal caveat.
- `expected_flags = pii_block` requires the Safety check to pass.

Aggregating human, judge, or explicitly enabled regression labels yields groundedness pass rate, correct-refusal rate, no-context violations, and RAG Reliability Index. Retrieval-only runs can safely produce retrieval/refusal evidence, but caveat quality, exact refusal wording, PII leakage, and final answer quality require full-answer or human/judge review.

## Run Modes

### Manual Full Run

Used by Gil or maintainers with the bot-owned Gemini key and Discord webhook configured.

Runs the full active-call workflow:

```text
question -> retrieval -> rerank -> dedupe -> context assembly -> Gemini -> Discord-safe response -> labels/review
```

### Manual Retrieval-Only Run

Used by maintainers and AltCtrlDeliver without Gemini or Discord credentials.

Runs:

```text
question -> retrieval -> rerank -> dedupe -> context assembly -> result row
```

This is the default mode for debugging retrieval quality, false refusals, missed refusals, selected context, and threshold behavior.

### CI Run

Used after PRs and material changes.

The default CI mode should be retrieval-only and no-secret:

- no Gemini API key
- no Discord webhook
- no production server access
- deterministic input file
- durable artifact with run summary and per-case results

Full-answer CI can be added later using bot-owned repository secrets, not personal keys.

## Maintenance Rules

- Prompts stay fixed unless the team intentionally versions the set.
- Labels are behaviors, not frozen answers.
- Revalidate no-context labels after major re-indexes because new logs can turn a refusal case into an answer case.
- Keep `contamination_risk = review` cases visible so the team knows which results may be trivial retrieval.
- Grow additively as production feedback reveals new failure modes.

## Notes

- Questions are original phrasings, not verbatim member messages.
- No real member PII is embedded. PII test cases use `<member_handle>` placeholders.
- Refusal labels are provisional relative to the indexed corpus. If the corpus grows, revalidate before treating them as hard failures.

## Related

- [evaluation-and-feedback-scoring-design.md](evaluation-and-feedback-scoring-design.md)
- [retrieval-context-prompt-contracts.md](retrieval-context-prompt-contracts.md)
- [Observability design.md](Observability%20design.md)
- [n8n execution plan.md](n8n%20execution%20plan.md)
