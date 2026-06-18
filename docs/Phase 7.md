# Phase 7: Context Assembly And Prompt Contract

## Status

Implementation branch: `phase-7-context-prompt-contract`

Workflow:

`workflows/n8n/rag-active-call-phase-7-context-prompt-contract.json`

## Goal

Phase 7 makes the LLM handoff explicit and contract-compliant after retrieval, reranking, and dedupe have selected candidate chunks.

Phase 6 proved the retrieval mechanics. Phase 7 separates context assembly into its own traceable step so the workflow can show exactly what evidence Gemini received, why chunks were kept or dropped, and whether the context budget gate passed.

## What Changed

- Added `Assemble Context Contract` after `Build Dedupe And Context Decision`.
- The new node builds the context block using the exact field structure from `retrieval-context-prompt-contracts.md`.
- The context-token budget gate now lives in the context assembly step.
- The workflow defaults to a `1200` context-token budget to match the contract.
- `Context Found?`, `Prepare Gemini Request`, `Prepare Discord Refusal`, and the Phoenix retrieval/context checkpoint now read from `Assemble Context Contract`.
- Retrieval result rows are regenerated after context assembly so `selected_for_context` reflects the final post-budget context.

## Context Block Contract

Each selected chunk is rendered as:

```text
--- Context chunk {n} of {total} ---
Channel:      #{channel}
Thread:       {thread_name or "N/A"}
Date range:   {start_ts[:10]} to {end_ts[:10]}
Authors:      {comma-joined authors}
Score:        {reranker_score, 2 decimal places}
Message IDs:  {comma-joined message_ids}
Discord link: https://discord.com/channels/853099205206999050/{channel_id}/{first_message_id}

{chunk text}
```

## Observability

Phase 7 emits Phoenix retrieval/context checkpoint spans for:

- `qdrant.query_completed`
- `retrieval.stage1_gate_passed` or `retrieval.stage1_gate_refused`
- `rerank.completed` or `rerank.low_confidence`
- `dedupe.completed`
- `context.assembled`
- `context.overflow` or `context.insufficient`

The context spans include:

- `context_contract_version`
- `prompt_hash`
- `selected_context_count_before_budget`
- `selected_context_count`
- `context_token_budget`
- `context_token_estimate_before_budget`
- `context_token_estimate`
- `context_dropped_for_budget_count`
- `context_overflow_detected`
- `refusal_reason`

Retrieval rows also include context assembly metadata in the row payload for debugging selected and dropped candidates.

## Success Criteria

A known supported query should:

- pass Stage 1 retrieval
- pass reranking
- pass dedupe
- assemble at least three context chunks within the token budget
- send Gemini a structured context block
- produce either a cited answer or a valid grounded refusal
- show Phase 7 context spans in Phoenix under `discord-rag-bot-phase-7`

A no-context or over-budget query should:

- refuse before Gemini when fewer than three context chunks remain after budget trimming
- use `context_token_budget_insufficient` as the refusal reason for budget failures
- show `context.insufficient` in Phoenix

## Known Notes

The stricter `1200` context-token budget may cause more conservative refusals than Phase 6. That is intentional for contract alignment and should be calibrated during regression testing.
