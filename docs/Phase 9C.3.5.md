# Phase 9C.3.5: Incremental Run State And Observability

**Scope:** Add durable, auditable incremental-run coordination without
claiming pending work or mutating Qdrant.

## Architecture Contract

n8n is the only lifecycle decision-maker. Postgres is the durable source of
truth and atomically enforces n8n's requested transitions. Phoenix receives
correlated diagnostic spans after the Postgres transaction commits. Python
validates deterministic shadow plans but never advances runtime lifecycle
state.

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
  replacement, snapshot, regression, failure, and rollback evidence.
- `rag_incremental_run_events`: append-only transition records with a unique
  per-run idempotency key.
- `rag_runtime_state`: one revisioned serving-state row per collection.
- `rag_active_execution_leases`: bounded shared-core execution leases used by
  the future drain gate.

It also adds the source corpus identity to replacement plans and extends the
plan lifecycle with `invalidated` and `applied`.

The migration is additive and safe to rerun. It seeds
`tpm_unite_history` as `serving` only when no runtime row already exists.

## Transaction And Lease API

n8n calls database functions rather than issuing independent state and event
updates:

- `rag_create_incremental_run(...)`
- `rag_transition_incremental_run(...)`
- `rag_acquire_execution_lease(...)`
- `rag_heartbeat_execution_lease(...)`
- `rag_release_execution_lease(...)`
- `rag_count_live_execution_leases(...)`

Transitions lock the collection runtime row, compare the expected state and
revision, validate the paired run/runtime transition, update state, and append
the event in one transaction. Replaying the same idempotency key returns the
original event; reusing it for different transition data fails closed.

Lease acquisition locks the same runtime row and succeeds only while the
collection is `serving`. This closes the race between starting a normal RAG
execution and entering `draining`. Leases have both a renewable expiry and a
hard maximum lifetime so abandoned n8n executions cannot block draining
forever.

## Shadow-Validation Contract

`ingestion.incremental_planner` persists `shadow_validated` only when:

- the source corpus version and manifest digest match the current healthy
  corpus;
- deterministic replanning reproduces the plan ID and digest;
- every ready replacement has exact point, ownership, and text-digest evidence;
- every replacement chunk was embedded by the declared production model;
- embedded and replacement counts match and every vector is 768 dimensions;
- fixture equivalence passes;
- the artifact reports zero Qdrant mutations.

Incomplete evidence remains `planned`. Deferred-only work remains `deferred`.
Contradictory or stale evidence fails closed.

Simulation mode creates a durable run and append-only event evidence while
leaving runtime state `serving`, pending work untouched, and Qdrant read-only.

## n8n Workflows

`RAG Incremental Coordinator - Phase 9C.3.5` is manual and inactive by
default. It creates a durable simulation run, reads the committed event, emits
an OTLP span carrying `incremental_run_id` and `incremental_event_id`, and
returns the authoritative Postgres state. A Phoenix delivery failure does not
roll back the Postgres record.

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

- a simulation run whose summary and append-only events remain queryable;
- matching `incremental_run_id` and event ID in Phoenix;
- unchanged pending-work and Qdrant counts before and after simulation;
- the complete Phase 8 retrieval-only regression at the accepted baseline.
