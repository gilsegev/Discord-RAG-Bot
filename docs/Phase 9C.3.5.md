# Phase 9C.3.5: Incremental Run State And Observability

**Scope:** Add durable, auditable incremental-run coordination without
claiming pending work or mutating Qdrant.

## Architecture Contract

n8n is the only lifecycle decision-maker. Postgres is the durable source of
truth for current state and outcome and provides narrow atomic operations for
closing and reopening the serving gate. Phoenix receives correlated diagnostic
spans after the Postgres transaction commits and is the detailed phase
timeline. Python validates deterministic shadow plans but never advances
runtime lifecycle state.

Phase 9C.3.5 must not:

- move the production runtime out of `serving`;
- claim or complete `rag_pending_chunk_work`;
- upsert, delete, or change Qdrant points or payloads;
- change active chunk-manifest ownership;
- post a maintenance response to Discord.

Those behaviors remain Phase 9C.4 work.

## Database Migration

Apply the prerequisite Phase 9C.2 and 9C.3 migrations first, then:

```bash
psql "$RAGBOT_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f deploy/phase0/sql/10-phase9c35-run-state-observability-migration.sql
```

The migration adds:

- `rag_incremental_runs`: permanent run summaries and future nullable
  phase results/timestamps, replacement, snapshot, regression, failure, retry,
  and rollback evidence.
- `rag_runtime_state`: one revisioned `serving`, `draining`, or `maintenance`
  row per collection.
- `rag_active_execution_leases`: bounded shared-core execution leases used by
  the future drain gate.

It also adds source-corpus identity to replacement plans. A stale plan is
rejected at application time and the rejection is recorded on its run; the plan
does not need another lifecycle status.

The migration is additive and safe to rerun. It seeds
`tpm_unite_history` as `serving` only when no runtime row already exists.

## Run, Maintenance, And Lease API

n8n calls explicit database operations rather than a general-purpose transition
engine:

- `rag_create_incremental_run(...)`
- `rag_update_incremental_run(...)`
- `rag_fail_incremental_run(...)`
- `rag_begin_incremental_drain(...)`
- `rag_enter_incremental_maintenance(...)`
- `rag_exit_incremental_maintenance(...)`
- `rag_acquire_execution_lease(...)`
- `rag_heartbeat_execution_lease(...)`
- `rag_release_execution_lease(...)`
- `rag_count_live_execution_leases(...)`

`rag_begin_incremental_drain` locks the runtime row, verifies the expected
revision and source corpus, closes the serving gate, and assigns the run in one
transaction. It does not wait inside that transaction. Already-admitted online
RAG work keeps its lease while new lease acquisition is denied.

`rag_enter_incremental_maintenance` succeeds only when that run still owns the
draining state and no live leases remain. `rag_exit_incremental_maintenance`
records the outcome and reopens serving. Exact duplicate calls return the
already-achieved state without an event table or idempotency key.

Lease acquisition locks the same runtime row and succeeds only while the
collection is `serving`. This closes the race between starting a normal RAG
execution and closing the serving gate. Draining means waiting only for online
RAG work that already passed the gate and may be using Qdrant, the reranker, or
Gemini. Discord capture continues. Leases have both a renewable expiry and a
hard maximum lifetime so abandoned n8n executions cannot block draining
forever.

## Shadow-Validation Contract

`ingestion.incremental_planner` persists `shadow_validated` only when:

- the source corpus version and manifest digest match the current healthy
  corpus;
- every replacement chunk was embedded by the declared production model;
- embedded and replacement counts match and every vector is 768 dimensions;
- the observed embedding model/version matches the declared version;
- the artifact reports zero Qdrant mutations.

Incomplete evidence remains `planned`. Deferred-only work remains `deferred`.
Contradictory or stale evidence fails closed.

Deterministic replanning, exact ownership/text digests, affected-scope
selection, and fixture equivalence remain automated planner tests. They are not
persisted production attestations.

Simulation mode creates a durable run summary while leaving runtime state
`serving`, pending work untouched, and Qdrant read-only.

## n8n Workflows

`RAG Incremental Coordinator - Phase 9C.3.5` is manual and inactive by
default. It creates or updates a durable simulation run, emits an OTLP span
carrying `incremental_run_id`, and returns the authoritative Postgres state. A
Phoenix delivery failure does not roll back the Postgres record.

The shared RAG core acquires a bounded lease before embedding and releases it
on normal result paths. Phase 9C.3.5 keeps runtime state at `serving`, so this
instrumentation does not introduce maintenance behavior or duplicate the
active/passive/regression RAG path.

## Validation

Static and unit checks:

```bash
python -m unittest ingestion.test_incremental_planner
node scripts/test-phase9c3-planner.js
node scripts/test-phase9c35-workflows.js
node -e "for (const f of require('fs').readdirSync('workflows/n8n')) JSON.parse(require('fs').readFileSync('workflows/n8n/' + f, 'utf8')); console.log('workflow json ok')"
```

Postgres integration checks use a uniquely named schema and drop only that
schema afterward:

```bash
DATABASE_URL="$RAGBOT_DATABASE_URL" \
  python scripts/test-phase9c35-postgres.py

DATABASE_URL="$RAGBOT_DATABASE_URL" \
  python scripts/test-phase9c35-e2e.py
```

Acceptance additionally requires:

- a simulation run whose summary remains queryable after reconnecting to
  Postgres;
- matching `incremental_run_id` in Phoenix;
- unchanged pending-work and Qdrant counts before and after simulation;
- the complete Phase 8 retrieval-only regression at the accepted baseline.
