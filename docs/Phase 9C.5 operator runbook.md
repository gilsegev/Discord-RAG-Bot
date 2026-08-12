# Phase 9C.5 Scheduled Incremental Operations

## Safety state

The scheduler has two independent database locks:

- `schedule_enabled` controls recurring execution.
- `catchup_completed` proves that the Phase 9C.6 historical catch-up finished.

Both must be true before a scheduled attempt can plan or run. Phase 9C.5 is
deployed with both false. Do not enable either as part of Phase 9C.5.

Discord capture is never disabled. A permitted run uses the existing Phase
9C.4 coordinator to gate new retrievals only during its short maintenance
window.

## Inspect current state

```sql
SELECT * FROM rag_incremental_schedule_config;
SELECT * FROM rag_incremental_run_reports ORDER BY started_at DESC LIMIT 20;
SELECT * FROM rag_incremental_operator_alerts ORDER BY created_at DESC LIMIT 20;
SELECT * FROM rag_runtime_state;
```

The expected pre-9C.6 state is `schedule_enabled=false`,
`catchup_completed=false`, and runtime `serving`.

## Dry run

Call the `rag-incremental-schedule-phase-9c5` webhook with `mode=dry_run`.
Before Phase 9C.6 it returns `blocked` with `phase9c6_catchup_required`. After
catch-up it returns `ready` when chunks can be built or `no_work` when all
pending messages are legitimately deferred. Every dry run reports zero Qdrant
mutations. Repeating the same attempt ID is idempotent.

## Enabling after Phase 9C.6

Only after the 9C.6 acceptance evidence is recorded:

1. Set `catchup_completed=true`.
2. Confirm the cron, timezone, message/chunk limits, and time budgets.
   Keep `PHASE9C5_CRON` in n8n aligned with the recorded cron expression; the
   default for both is `0 3 * * *` (03:00 UTC). The scheduled controller sets
   its workflow timezone explicitly to `UTC`; do not rely on the n8n instance
   timezone.
3. Activate the secret-protected scheduled-run workflow. It has no cron trigger,
   but its production webhook must be registered before the controller can
   dispatch manual or scheduled execution.
4. Run one manual dry run and inspect its plan/report. A deferred-only plan is a
   normal `no_work` result, not a blocked or failed attempt.
5. Run one operator-approved manual execution through the scheduled controller.
6. If it completes with matching structural and regression results, set
   `schedule_enabled=true` and activate the scheduled controller.

Never bypass the `rag_incremental_schedule_config` locks or call the scheduled
runner directly.

## Failure handling

- Before maintenance: fix the reported guard or plan problem and create a new
  attempt. Qdrant and serving are unchanged.
- Drain timeout: `rag_cancel_incremental_drain` returns runtime to `serving`;
  inspect leases before retrying.
- After mutation: the scheduled runner calls the Phase 9C.4 rollback and exit
  path. Do not reopen manually until rollback is recorded complete.
- `review_needed` or runtime not `serving`: keep scheduling disabled, inspect
  the run report plus Phoenix trace, and escalate to an operator.

Alerts are durably queued in Postgres. When
`INCREMENTAL_ALERT_WEBHOOK_URL` is configured, the alert workflow delivers
them to the private operations destination and records delivery status.

## Production activation record

Production scheduling was enabled on August 12, 2026 with cron `0 3 * * *` and
workflow/database timezone `UTC`. Supervised run
`scheduled-phase9c5-prod-supervised-20260812-utc-r2-11215` passed baseline and
post-update regression plus structural verification, processed four messages
into two new Qdrant points, and returned runtime to `serving`. The final enabled
dry run `phase9c5-prod-final-enabled-validation-20260812` returned normal
`no_work` for 21 deferred messages with zero mutations.
