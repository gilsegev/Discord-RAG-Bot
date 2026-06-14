# Evaluation — Regression Question Set

Curated evaluation data for the **Evaluation & Feedback Scoring** workstream. The full approach — rubric, launch gates, metrics, phases — lives in [`docs/evaluation-and-feedback-scoring-design.md`](../docs/evaluation-and-feedback-scoring-design.md). This folder holds the data that approach runs on.

## Why this exists
The v1 bot was deprecated for ungrounded, generic answers. This set is the fixed yardstick that proves the new bot is grounded and refuses appropriately — used both as the **launch gate** and as the **regression check** re-run on every prompt / retrieval / model / data change. It also serves as the calibration set for reranker thresholds (see `docs/retrieval-context-prompt-contracts.md`, Open Question 2).

## Contents
| File | Role |
|---|---|
| `regression_questions.jsonl` | **Canonical** set — one eval case per line. Single source of truth. |
| `export_review_csv.py` | Regenerates a Sheets-friendly CSV for adjudication. The CSV is disposable; the JSONL is canonical. |
| `README.md` | This file. |

## Case schema
| Field | Meaning |
|---|---|
| `id` | Stable case ID (`RQ-NNN`). Never reused. |
| `category` | `happy_path` · `nuanced_subjective` · `personal_context` · `no_context_refusal` · `adversarial_pii` |
| `channel_scope` | `all`, or a specific channel to exercise the retrieval channel filter |
| `question` | The prompt sent to the bot |
| `expected_action` | `answer` or `refuse` |
| `expected_caveat` | `yes` if a nuance / temporal / personal caveat is required, else `no` |
| `expected_flags` | `pii_block` if the answer must not surface PII, else `none` |
| `contamination_risk` | `review` if the question is close to a single indexed thread (may test trivial retrieval), else `low` |
| `source_area` | Corpus area the case relates to; carries `PROVISIONAL` notes where a label depends on the full index |
| `expected_behavior` | Human-readable behavior label — **not** a frozen answer |

## Categories (v1 — 45 cases)
| Category | Count | Expected action |
|---|---|---|
| `happy_path` | 16 | answer (grounded) |
| `nuanced_subjective` | 8 | answer + required caveat |
| `personal_context` | 5 | answer as community wisdom + caveat (never authoritative) |
| `no_context_refusal` | 10 | refuse with the exact string |
| `adversarial_pii` | 6 | refuse / block PII / resist injection |

Totals: 29 answer · 16 refuse · 13 caveat-required · 4 PII-block.

## How it maps to the rubric
Each case carries the *expected behavior*, not a gold answer. Scoring a bot run against a case produces Pass/Fail per rubric dimension, written to `rag_eval_labels` (`transaction_id`, `dimension`, `label`, `failure_type`, `source`, `labeler`):

- `expected_action` → **Tone/Refusal** dimension. A refusal must use the exact string from the prompt contract.
- When `expected_action = answer`, the response must pass **Groundedness** and **Answer relevance**.
- `expected_caveat = yes` → the nuance/temporal caveat must be present.
- `expected_flags = pii_block` → the **safety** check must be clean.

Aggregating those labels yields groundedness pass rate, correct-refusal rate, etc. — the launch gates and RRI. Observability owns the rollup; this workstream produces the labels.

## Using it
1. **Adjudicate** (before merge, and before trusting any results): `python eval/export_review_csv.py` → open the CSV → walk `expected_action` / `expected_behavior` with Gil, or post the CSV for the community. Focus on the refusals and the `contamination_risk = review` rows — that's where the live index may differ from the labels.
2. **Run & label** (Phase 0): run each question through the bot, grade the response against the rubric, write labels to `rag_eval_labels` with `source = human`.
3. **Automate** (Phase 2): an LLM-as-judge (Gemini, validated against human labels first) scores the set on every change. The harness lands in this folder later (`run_eval.py`, `judge.py`).

## Maintenance — the set must survive corpus growth
- **Prompts stay fixed.** The value is a stable yardstick; churning questions loses the trend line.
- **Labels are behaviors, not frozen answers** — robust as grounded content evolves.
- **Re-validate refusals on every major re-index.** New logs can turn "no context" into "context exists," flipping the correct answer. Refusal labels here are provisional relative to the full Qdrant index (this v1 was built from a 17-file sample).
- **Hold out or rephrase `contamination_risk = review` cases** so the set isn't testing trivial retrieval.
- **Grow additively.** Add coverage as new channels/topics get indexed and as production 👎s arrive; keep old cases for continuity. Version this file.

## Notes & assumptions
- Questions are original phrasings, not verbatim member messages.
- No real member PII is embedded. PII test cases use a `<member_handle>` placeholder — substitute a real handle only if you intend to exercise that case.
- `no_context_refusal` labels are provisional against the full index; `RQ-035` (Stripe) is explicitly flagged, and `RQ-021` (dated TPM Summit) is included as a temporal-honesty case.

## Related
- `docs/evaluation-and-feedback-scoring-design.md` — rubric, gates, metrics, phases
- `docs/retrieval-context-prompt-contracts.md` — retrieval thresholds, exact refusal string, system prompt
- `docs/Observability design.md` — `rag_eval_labels`, weekly rollup, feedback correlation
