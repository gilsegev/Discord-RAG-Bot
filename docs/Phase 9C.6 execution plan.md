# Phase 9C.6 Execution Plan

## Outcome

Perform one controlled catch-up that adds every corpus-eligible Discord message
after the current healthy corpus boundary and through a fixed cutoff. Prove that
the source history is complete before changing Qdrant, and leave messages after
the cutoff for the Phase 9C.5 scheduled path.

This phase is a one-time migration. It must reuse the Phase 9C.4 planner,
maintenance coordinator, rollback, structural checks, and regression path. It
must not create another Qdrant mutation path.

## Starting point

Production evidence captured on August 12, 2026 shows:

- active-corpus boundary: Discord message `1529684708294787083`, approximately
  `2026-07-23 03:00:54 UTC`
- first continuously captured message: `2026-07-26 18:35:40 UTC`
- known unproven interval: approximately 3 days and 15 hours
- latest Phase 9C.5 dry run: 176 bounded pending messages
- minimum gap checklist: 20 real Discord message IDs in
  [phase9c6-known-gap-message-ids.csv](phase9c6-known-gap-message-ids.csv)
- recurring schedule and catch-up locks: `schedule_enabled=false` and
  `catchup_completed=false`

These values are evidence, not live assumptions. Refresh and record every count
before execution.

## Execution sequence

### 1. Freeze the migration boundary

1. Confirm the active manifest and healthy corpus boundary in Railway Postgres.
2. Choose and persist one `cutoff_capture_sequence` and its Discord timestamp.
3. Keep the Phase 9C.5 schedule disabled. Messages received after the cutoff
   continue to be captured, but are excluded from this migration.
4. Record pre-run counts for captured messages, pending work, manifest rows,
   Qdrant points, and corpus/runtime versions.

**Gate:** the boundary and cutoff are immutable for the remainder of the run.

### 2. Prove or recover source coverage

1. Produce DiscordChatExporter exports for every corpus-eligible channel,
   covering the corpus boundary through the fixed cutoff. A partial set of
   channels is not sufficient.
2. Add a deterministic reconciliation command that compares the exports with:
   - the active chunk manifest
   - `rag_discord_messages`
   - `rag_pending_chunk_work`
   - the 20-ID known-gap checklist
3. Write a machine-readable report listing every exported message as already
   represented, captured/pending, excluded with a reason, or missing.
4. Never import the synthetic `replay-*` transaction IDs.
5. If eligible messages are missing, import them idempotently through the
   durable capture contract, then rerun reconciliation.

**Gate:** every eligible exported message through the cutoff is represented or
captured, all 20 checklist IDs have a durable outcome, and no unexplained gap
remains. If this cannot be proved, stop without changing Qdrant.

### 3. Build and validate the catch-up plan

1. Run the Phase 9C.4 planner with the fixed cutoff and persist its shadow plan.
2. Verify the plan includes every eligible pre-cutoff pending row and no
   post-cutoff row.
3. Run the full retrieval regression to establish the immediate pre-run
   baseline.
4. Take a full Qdrant snapshot and verify that it is restorable.
5. Run all maintenance-admission, lease-drain, capacity, and stale-plan checks.

**Gate:** the plan is `shadow_validated`, the baseline passes, the full snapshot
exists, and the runtime is healthy. Otherwise stop before maintenance.

### 4. Apply through the proven coordinator

1. Enter maintenance through the Phase 9C.4 coordinator; do not toggle the
   pipeline or schedule as an ad hoc step.
2. Drain active retrieval leases.
3. Apply the deterministic replacement plan, update the manifest, and run
   structural verification.
4. On any failure, use the coordinator's rollback path and keep serving closed
   until the restored state is verified.

**Gate:** Qdrant and the active manifest agree, the new corpus version is
healthy, and no eligible pre-cutoff work is silently pending or claimed.

### 5. Validate and close the migration

1. Run the complete post-change regression and compare it with the accepted
   pre-run baseline.
2. Run at least one full question-to-answer retrieval that is expected to use a
   newly added message, and preserve the selected message/chunk evidence.
3. Reconcile before/after captured-message, work-row, manifest, Qdrant-point,
   corpus-version, regression, maintenance-duration, and rollback counts.
4. Reopen serving only after structural and regression gates pass.
5. Set `catchup_completed=true` only after all evidence is stored. Leave
   `schedule_enabled=false`; enabling recurring operation remains a separate,
   explicit operator action under the Phase 9C.5 runbook.

**Gate:** the migration report is complete, the known-gap checklist is fully
reconciled, retrieval proof passes, and production is serving the verified new
corpus.

## Deliverables

- complete, private Discord channel exports for the boundary-to-cutoff window
- deterministic coverage/reconciliation and idempotent recovery-import tooling
- completed known-gap checklist with durable reasons
- fixed-cutoff shadow plan and preflight report
- verified full Qdrant snapshot
- pre/post full regression comparison and targeted retrieval proof
- permanent migration report containing counts, versions, timings, and outcome
- `catchup_completed=true` only on success; schedule still disabled

## Operator assistance

The only expected manual dependency is access to create the complete
DiscordChatExporter channel set if the existing bot credentials cannot do it.
No Discord token or export containing private message content may be committed.
All Railway, Postgres, n8n, Qdrant, planner, snapshot, and validation work can be
performed through the existing private service access.

## Rollback and stop rules

- Before maintenance: stop cleanly; Qdrant remains unchanged.
- During or after replacement failure: use Phase 9C.4 rollback, verify the old
  manifest/corpus pair, then reopen serving.
- Regression mismatch: keep serving closed and roll back unless an explicit,
  reviewed baseline change explains the result.
- Coverage uncertainty, missing export channels, unresolved checklist IDs, or
  a stale cutoff: do not start the mutation.
