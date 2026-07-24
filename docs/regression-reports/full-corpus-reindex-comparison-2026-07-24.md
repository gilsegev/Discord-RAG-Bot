# Full-corpus reindex comparison — 2026-07-24

## Corpus and index

| Metric | Before | After | Change |
|---|---:|---:|---:|
| Export files | 21 | 46 | +25 |
| Exported messages | 25,369 | 78,920 | +53,551 (+211.1%) |
| Eligible parsed messages | — | 77,555 | — |
| Qdrant chunks/points | 9,519 | 32,756 | +23,237 (+244.1%; 3.44× total) |

The parser excluded 1,365 exported messages through its existing channel,
bot, system-message, and noise eligibility rules. The full rebuild completed
in 138.4 minutes and verified all 32,756 expected points with zero missing.

Before deletion, a Qdrant snapshot was downloaded outside the Qdrant data
volume and verified with SHA-256:

```text
bfc1e9e634f355d69756ea4f45bd956fb07dbea0b776c58ecb9f2614e2fba14a
```

## Retrieval-only regression

Both runs used the same 45 canonical cases and the same settings:

```json
{
  "cases": "all",
  "mode": "retrieval_only",
  "allow_gemini": false,
  "allow_discord_post": false,
  "write_eval_labels": false
}
```

Canonical question-set SHA-256:
`fd5508a9a43a0debf41d68643ef167372a22bb00484b0110a2b64ebcd0c095db`.

| Metric | Before | After | Change |
|---|---:|---:|---:|
| Pass | 39/45 (86.7%) | 40/45 (88.9%) | +1 case; +2.2 pp |
| False refusal | 2/45 (4.4%) | 1/45 (2.2%) | -1 case; -2.2 pp |
| Review needed | 4/45 (8.9%) | 4/45 (8.9%) | unchanged |
| Expected-answer pass | 27/29 (93.1%) | 28/29 (96.6%) | +1 case; +3.5 pp |
| Batch wall time | 128 s | 132 s | +4 s (+3.1%) |
| Mean case trace latency | 98.24 s | 104.09 s | +5.84 s (+5.9%) |
| Median case trace latency | 113 s | 123 s | +10 s (+8.8%) |
| p95 case trace latency | 125 s | 130 s | +5 s (+4.0%) |
| Mean selected contexts | 2.89 | 3.36 | +0.47 (+16.3%) |
| Mean context tokens | 858.1 | 1,007.8 | +149.7 (+17.4%) |

Category outcomes:

| Category | Before | After |
|---|---|---|
| Happy path (16) | 15 pass, 1 false refusal | unchanged |
| Nuanced/subjective (8) | 8 pass | unchanged |
| Personal context (5) | 4 pass, 1 false refusal | 5 pass |
| No-context refusal (10) | 6 pass, 4 review needed | unchanged |
| Adversarial/PII (6) | 6 pass | unchanged |

The only outcome transition was `RQ-026` (“Should I accept a TPM offer that
doesn't include visa sponsorship?”), which moved from `false_refusal` to
`pass`. Before the rebuild it had no reranker candidates above threshold.
Afterward it selected four contexts (1,011 estimated tokens), including
community discussion of H-1B sponsorship and employers' visa preferences.

`RQ-010` remains the only false refusal. Its channel-scoped dependency
management query still has no reranker candidates above threshold in
`#tpm-tradecraft`.

The four unchanged `review_needed` cases are `RQ-033`, `RQ-034`, `RQ-035`,
and `RQ-037`.

## Interpretation

The larger corpus improved expected-answer recall and halved the false-refusal
count without increasing the review-needed set. The tradeoff was moderately
larger assembled contexts and a small latency regression.

This was a retrieval-only comparison. It validates retrieval/refusal
decisions, selected context, reranker behavior, and dedupe evidence. It does
not generate answer text and therefore does not validate final-answer
groundedness, wording, caveats, citations, or PII leakage. `RQ-026` should
receive human/full-answer review because its selected set contains relevant
visa evidence but also a less-direct Microsoft screening chunk.

Raw reports:

- `phase8-full-regression-before-full-corpus-2026-07-23.json`
- `phase8-full-regression-after-full-corpus-2026-07-24.json`
