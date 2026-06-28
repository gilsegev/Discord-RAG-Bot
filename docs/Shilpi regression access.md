# Shilpi Regression Access

## Purpose
Give AltCtrlDeliver / Shilpi a safe way to run Phase 8 retrieval-only regression batches without Gil's Gemini key, Discord webhook, or broad shell access to the Oracle server.

## Access Model
Server user:

```text
shilpi_eval
```

Access type:

- SSH tunnel only
- no interactive shell
- no pty
- no agent forwarding
- allowed local forwards:
  - n8n: `127.0.0.1:5679`
  - Phoenix: `127.0.0.1:6006`

Installed public key:

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKP1NlhMOpXpl0P0TZgxuMH5hKMdMBVvvEEnuJ2FUSRi shilpi-tpm-rag-eval
```

## Open The Tunnel
From Shilpi's machine:

```bash
ssh -N \
  -L 5679:127.0.0.1:5679 \
  -L 6006:127.0.0.1:6006 \
  shilpi_eval@discord-notifier.duckdns.org
```

If the private key is not in the default SSH location:

```bash
ssh -i /path/to/private/key -N \
  -L 5679:127.0.0.1:5679 \
  -L 6006:127.0.0.1:6006 \
  shilpi_eval@discord-notifier.duckdns.org
```

Keep this terminal open while using n8n or Phoenix.

## Run A Retrieval-Only Regression Batch
This path does not require Gemini or Discord credentials.

```bash
curl -s -X POST http://127.0.0.1:5679/webhook/rag-regression-batch \
  -H "Content-Type: application/json" \
  -d '{
    "cases": "RQ-001,RQ-036",
    "mode": "retrieval_only",
    "allow_gemini": false,
    "allow_discord_post": false,
    "write_eval_labels": false,
    "requested_by": "shilpi"
  }'
```

Run the full set:

```bash
curl -s -X POST http://127.0.0.1:5679/webhook/rag-regression-batch \
  -H "Content-Type: application/json" \
  -d '{
    "cases": "all",
    "mode": "retrieval_only",
    "allow_gemini": false,
    "allow_discord_post": false,
    "write_eval_labels": false,
    "requested_by": "shilpi"
  }'
```

Useful filters:

```json
{ "category": "no_context_refusal" }
```

```json
{ "limit": 5 }
```

## Review Results
n8n UI:

```text
http://127.0.0.1:5679
```

Workflow:

```text
RAG Regression Batch Runner - Phase 8
```

Phoenix UI:

```text
http://127.0.0.1:6006
```

Phoenix project:

```text
discord-rag-bot-phase-8-regression
```

The webhook response includes:

- run ID
- pass/fail/review counts
- per-case outcome
- transaction trace
- selected chunks
- reranker scores
- dedupe evidence
- candidate report

## Current Reference Report
The latest full Phase 8 retrieval-only report is checked in here:

```text
docs/regression-reports/phase8-full-regression-report-2026-06-27.json
```

## Known Limitations
Retrieval-only mode can validate:

- retrieval/no-context decisions
- selected context
- rerank scores
- dedupe behavior
- false refusals and possible missed refusals

Retrieval-only mode cannot validate:

- final generated answer wording
- exact refusal string
- PII leakage in generated text
- citation quality in the final answer
- caveat quality

Those require full-answer mode, human review, or judge scoring.
