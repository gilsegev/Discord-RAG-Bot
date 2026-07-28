# Continuous Incremental Ingestion Design

**Status:** Accepted production design (merged in PR #24)

**Supersedes for production use:** recurring export-based ingestion

**Retains from the original incremental design:** bounded rechunking, durable state, verification, observability, regression gates, and full-rebuild recovery

**Post-PR #24 revision:** Phase 9C.3.5 separates run-state/observability from
production mutation; Phase 9C.4 accepts only fully `shadow_validated` plans and
retains per-run rollback snapshots for 14 days.

## 1. Decision Summary

Production incremental ingestion will use the existing Discord Gateway listener as
the source of new messages. It will not require recurring full-channel exports.

The MVP will:

1. capture new Discord messages and reply metadata durably in Postgres;
2. coalesce new messages into dirty conversation/window work items;
3. enter a short scheduled maintenance window during low traffic;
4. stop RAG retrieval and generation while Qdrant is updated;
5. rebuild only the complete affected conversations or time windows;
6. verify the resulting Qdrant and manifest state;
7. run the complete Phase 8 regression suite;
8. reopen the RAG service only after the update and validation pass.

Message edits, message deletions, and recovery of messages missed during an
unresumable listener outage are explicitly deferred beyond MVP.

## 2. Why a Maintenance Window

Keeping retrieval live during partial Qdrant replacement requires coordination
across Postgres and Qdrant, which cannot participate in one atomic transaction.
It introduces transient duplicates, incomplete replacement windows, complicated
rollback behavior, and more difficult incident diagnosis.

The expected update volume is small enough that a short low-traffic maintenance
window is the safer MVP tradeoff. During maintenance:

- the Discord listener continues capturing new events into Postgres;
- active calls receive a clear temporary-maintenance response or are queued;
- passive RAG processing is paused;
- no retrieval queries read Qdrant while replacement is in progress.

This removes user-visible partial-index states. It does not make Postgres and
Qdrant transactional, so the update job must remain idempotent and recoverable,
but it substantially lowers consistency and implementation risk.

### Complexity and risk assessment

| Design | Complexity | Initial operational risk | User impact |
|---|---|---|---|
| Live in-place Qdrant replacement | High | Medium to high | No planned downtime |
| Short maintenance-window replacement | Medium | Low to medium | Brief planned unavailability |
| Full rebuild for every update | Low implementation complexity, high operational cost | Low consistency risk | Long downtime |

The maintenance-window design is recommended for MVP.

### Measured capacity and Railway budgeting

The retired Oracle host measured approximately 64 chunks per minute during a
July 2026 full rebuild. That result remains a conservative historical benchmark,
not a production capacity guarantee. Production incremental jobs now run on
Railway, where CPU and memory allocation, model startup, and shared-disk I/O can
produce different throughput.

This confirms two design constraints:

- recurring full-corpus rebuilds or bulk re-exports are not a sustainable
  production update path;
- daily incremental maintenance is viable only when affected-region rechunking
  remains small and bounded.

Approximate embedding time at the observed rate, excluding model startup,
planning, verification, and regression:

| Replacement chunks | Approximate embedding time |
|---:|---:|
| 64 | 1 minute |
| 320 | 5 minutes |
| 640 | 10 minutes |
| 1,920 | 30 minutes |

Before maintenance scheduling is enabled, measure chunking and embedding
throughput on the deployed Railway services and store that result with the
validation evidence. The scheduler must estimate replacement count and duration
from the Railway measurement before entering maintenance. If the estimate
exceeds the configured budget, the run remains pending for admin review or is
split into ownership-safe batches. Workstation acceleration is useful for an
exceptional baseline migration, but is not a steady-state production dependency.

## 3. Scope

### MVP scope

- `MESSAGE_CREATE` events from permitted server channels and threads
- durable capture before active/passive relevance routing
- direct Discord reply references
- conversation identity using known reply chains
- time-window grouping for non-reply messages
- coalesced offline work items
- scheduled maintenance mode
- affected-region rechunking and embedding
- Qdrant replacement using a durable chunk manifest
- structural verification
- complete Phase 8 regression run
- update audit records and Phoenix spans

### Deferred scope

- `MESSAGE_UPDATE`
- `MESSAGE_DELETE` and bulk delete events
- automatic REST history catch-up
- automated ingestion of messages missed during an unresumable listener outage
- zero-downtime Qdrant updates
- real-time per-message embedding

## 4. Runtime Architecture

```text
Discord MESSAGE_CREATE
        |
        v
Discord Gateway listener
        |
        +--> durable message capture in Postgres
        |        |
        |        +--> dirty conversation/window queue
        |
        +--> existing active/passive intake and response routing

Scheduled low-traffic update
        |
        v
Enter maintenance mode
        |
        v
Drain in-flight RAG executions
        |
        v
Claim dirty work items
        |
        v
Load complete affected regions from Postgres
        |
        v
Chunk -> embed -> replace affected Qdrant points
        |
        v
Structural verification
        |
        v
Complete Phase 8 regression suite
        |
        +--> pass: mark corpus healthy and leave maintenance mode
        |
        +--> fail: keep maintenance mode, mark review_needed, alert admin
```

The listener must remain available during maintenance so messages arriving during
the update are not lost. Those messages are stored for the next update unless
they were included before the current batch cutoff.

## 5. Durable Message Capture

The listener currently forwards messages to n8n through a bounded in-memory
queue. MVP adds a durable capture write before answer-routing eligibility is
evaluated.

Proposed canonical table:

```sql
CREATE TABLE rag_discord_messages (
    message_id         TEXT PRIMARY KEY,
    guild_id           TEXT NOT NULL,
    channel_id         TEXT NOT NULL,
    channel_name       TEXT,
    thread_id          TEXT,
    parent_message_id  TEXT,
    root_message_id    TEXT,
    author_id_hash     TEXT NOT NULL,
    content            TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    captured_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    capture_run_id     TEXT NOT NULL
);
```

The exact schema may add safe message metadata already required by the chunker.
Raw author IDs and excluded-channel content must not be persisted.

Capture is idempotent on `message_id`. Bot, webhook, and system-message policy
must be explicit; content excluded from the historical corpus today should not
silently enter the new corpus.

## 6. Conversation and Window Tracking

### Replies

For a reply, persist `parent_message_id` from Discord's message reference.
Follow known parents in Postgres to determine the oldest known
`root_message_id`. The work key is:

```text
(channel_id, thread_id, root_message_id)
```

If the direct parent is already present in the baseline corpus manifest but not
in the live-message table, the baseline message-to-chunk map supplies its root or
conversation identity.

### Non-reply messages

Non-reply traffic is assigned to a provisional channel/thread time window using
the chunker's existing window rules. Adjacent dirty windows are merged before
processing so the chunker receives enough preceding and following context to
reproduce overlap correctly.

### Coalescing

The work queue has one active row per conversation/window key. Additional
messages expand the affected range and increment counters rather than creating
independent rechunk jobs.

```sql
CREATE TABLE rag_chunk_work_queue (
    work_key             TEXT PRIMARY KEY,
    channel_id           TEXT NOT NULL,
    thread_id            TEXT,
    root_message_id      TEXT,
    earliest_message_id  TEXT NOT NULL,
    latest_message_id    TEXT NOT NULL,
    pending_message_count INTEGER NOT NULL,
    status               TEXT NOT NULL,
    first_seen_at        TIMESTAMPTZ NOT NULL,
    last_seen_at         TIMESTAMPTZ NOT NULL,
    claimed_run_id       TEXT
);
```

## 7. Chunk Ownership Manifest

Aggregate per-file state is insufficient for targeted replacement. The system
needs a manifest connecting Qdrant points to their messages:

```sql
CREATE TABLE rag_chunk_manifest (
    point_id             TEXT PRIMARY KEY,
    logical_group_id     TEXT NOT NULL,
    channel_id           TEXT NOT NULL,
    thread_id            TEXT,
    root_message_id      TEXT,
    message_ids          TEXT[] NOT NULL,
    first_message_id     TEXT NOT NULL,
    last_message_id      TEXT NOT NULL,
    chunker_version      TEXT NOT NULL,
    embedding_version    TEXT NOT NULL,
    ingestion_run_id     TEXT NOT NULL,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_at        TIMESTAMPTZ
);
```

The final full export/rebuild seeds this manifest and is the baseline from which
continuous ingestion begins.

## 8. Maintenance-Window Update Procedure

Before this procedure, the plan must be `shadow_validated`. That requires
deterministic replanning, exact old/replacement ownership and text digests,
complete production-model embedding of every replacement chunk, embedded count
equal to replacement count, only 768-dimension vectors, declared version
agreement, fixture equivalence, and zero Qdrant mutations. A plan missing any
evidence remains `planned` and cannot be applied.

1. Create a durable incremental run and transition event in the `ragbot`
   Postgres database.
2. Record a fixed batch cutoff. Messages captured after it remain pending.
3. Revalidate the `shadow_validated` plan, then enable maintenance mode for
   active and passive RAG execution.
4. Allow already-running retrieval/generation executions to finish, with a
   bounded drain timeout.
5. Claim all eligible dirty work rows for the run.
6. Resolve every affected old point through `rag_chunk_manifest`.
7. Load the complete affected conversation/window, including required overlap.
8. Run the existing chunker and embedding model.
9. Confirm the old/replacement sets and persist rollback snapshots of affected
   vectors, payloads, manifest rows, and digests.
10. Upsert replacement Qdrant points.
11. Delete superseded Qdrant points that are not part of the replacement set.
12. Update the manifest and mark work rows processed.
13. Run structural verification.
14. Run the complete Phase 8 regression suite in retrieval-only mode.
15. If all gates pass, mark the corpus version `healthy`, complete the run, and
    disable maintenance mode.
16. If any gate fails, keep the corpus unavailable, mark the run
    `review_needed`, preserve evidence, and alert an admin.

The procedure must be safe to retry using the same run plan. The replacement set
must be deterministic for a fixed input, chunker version, and embedding version.
Affected-point rollback snapshots are retained for 14 days for every production
run. The first production canary also takes a full Qdrant snapshot. Snapshot
deletion is forbidden while a related run is `review_needed` or before the
replacement passes structural verification and regression.

## 9. Verification and Regression Gates

### Structural gate

- every replacement manifest point exists in Qdrant;
- every superseded point is absent from Qdrant;
- every processed message appears in at least one active chunk;
- active chunk message IDs are valid canonical or baseline messages;
- no unexpected active point IDs exist for the affected groups;
- Qdrant payload metadata matches the manifest;
- no claimed work row is left in an ambiguous state.

### Regression gate

Run all canonical cases described in
[Regression README.md](Regression%20README.md) after every update batch.

MVP uses retrieval-only mode:

```json
{
  "mode": "retrieval_only",
  "allow_gemini": false,
  "allow_discord_post": false,
  "write_eval_labels": false,
  "requested_by": "incremental_ingestion"
}
```

The run report must be associated with the ingestion run and corpus version.
Any failed case or `review_needed` outcome beyond the agreed baseline prevents
the corpus version from being marked healthy until reviewed.

## 10. Listener Downtime Policy

### MVP

Normal short disconnects rely on Discord Gateway resume and replay. MVP does not
implement REST history fetching and does not claim guaranteed recovery after an
unresumable session, extended outage, queue overflow, or failed delivery before
durable capture.

The listener must record:

- last successful durable capture time;
- disconnect and reconnect times;
- whether the Gateway session resumed;
- queue overflow or durable-write failures;
- suspected gap start and end.

### Future phase: manual gap review and bulk recovery

When the listener cannot resume or a gap is suspected:

1. create a corpus-gap issue for admin review;
2. mark the incident `review_needed`, consistent with the regression review
   vocabulary;
3. include affected time range, known channels/threads, last captured message
   IDs, and listener evidence;
4. ask an authorized admin to export only the missing period/messages;
5. ingest the recovery export through a controlled bulk-import path;
6. run structural verification and the complete regression suite.

This phase is intentionally outside MVP. No automatic history pulling is
required for MVP.

## 11. Failure and Recovery Policy

- Failure before maintenance mode leaves the current corpus available.
- Failure after maintenance begins keeps RAG unavailable until the run is
  retried, completed, or explicitly rolled back.
- New Discord messages continue to be captured throughout the failure.
- Work rows are never marked complete before Qdrant and manifest verification.
- The run stores old point IDs and replacement point IDs so an admin can diagnose
  or retry deterministically.
- A full rebuild remains the final recovery option.
- Affected-point rollback evidence is retained for 14 days after every
  production replacement run.

MVP favors a clearly unavailable service over serving a partially updated or
unvalidated corpus.

## 12. Phased Execution Plan

### Phase 9C.0 — Approve design and establish baseline

- Merge the reviewed production design in PR #24.
- Retain export fingerprinting only for baseline/backfill and recovery imports.
- Complete the final full export ingestion.
- Record the baseline corpus version, chunker version, embedding version, and
  full regression report.

**Gate:** baseline Qdrant and regression suite are healthy and reproducible.

### Phase 9C.1 — Durable `MESSAGE_CREATE` capture

- Add the canonical message and work-queue tables.
- Extend the listener event contract with reply/thread/timestamp fields.
- Persist permitted messages before active/passive classification.
- Make capture idempotent by message ID.
- Keep existing answer routing behavior unchanged.
- Add capture health metrics and failure alerts.

**Gate:** test traffic is durably recorded once, excluded content is absent, and
answer routing has no regression.

### Phase 9C.2 — Seed chunk ownership manifest

- Add the additive `rag_chunk_manifest` schema and the minimal
  ingestion-run/corpus-version state needed to identify the seeded baseline.
- Build the manifest from the current Qdrant payloads without deleting,
  replacing, or re-embedding any production point.
- Record deterministic logical ownership for every point using its channel,
  thread, reply-root where known, and bounded window identity otherwise.
- Preserve the current point ID, complete per-piece `message_ids`, first/last
  message IDs, chunker version, embedding version, and corpus version.
- Add indexed message-to-point, logical-group-to-point, and reply-root lookup
  paths for later planning.
- Make seeding restartable and idempotent: rerunning the same corpus version
  produces the same active manifest and cannot create duplicate ownership.
- Produce machine-readable structural verification and a dry-run summary before
  any write.

**Gate:** the seeded manifest has exactly one active row per production Qdrant
point; every manifest point exists in Qdrant; every Qdrant point is represented;
point payload metadata and message membership match; no point is mutated; and a
second seed run produces zero changes. Any payload that cannot be assigned
deterministically fails the seed instead of being guessed.

### Phase 9C.3 — Offline planner and shadow rechunking

**Implementation:** `ingestion/incremental_planner.py` and the additive
`09-phase9c3-shadow-plans-migration.sql`. Planning and shadow embedding are
strictly read-only toward Qdrant; pending work is not claimed in this phase.

- Coalesce reply conversations and non-reply windows.
- Produce deterministic old-point and replacement-point plans.
- Run chunking and embedding without modifying production Qdrant.
- Compare shadow output with expected full-chunker output for selected channels.
- Measure update duration to set a realistic maintenance window.
- Estimate replacement chunk count and projected Railway processing time before
  maintenance begins.

**Gate:** affected-region output matches full-chunker behavior for the validation
fixtures, no unrelated point is selected for replacement, and normal daily
traffic fits within the agreed maintenance budget at measured Railway
throughput.

### Phase 9C.3.5 — Incremental-run state and observability

- **Status:** Implemented; manual and non-mutating until Phase 9C.4.
- Add durable run summaries, append-only transition events, runtime serving
  state, and active-execution leases to the `ragbot` Postgres database.
- Make n8n the single coordinator and lifecycle-state writer.
- Emit correlated Phoenix spans using the durable incremental run ID.
- Record cutoff, plan/corpus IDs, timestamps, message/group/point counts,
  snapshot bytes/digest, phase durations, regression result, retries, failures,
  and rollback outcome.

**Gate:** transitions are transactional and idempotent, invalid transitions fail
closed, counts reconcile to source tables, and Phoenix traces link to the
durable Postgres run.

### Phase 9C.4 — Maintenance mode and production replacement

- Accept and revalidate only plans with complete `shadow_validated` evidence.
- Add the maintenance-mode gate to shared intake.
- Continue durable listener capture during maintenance.
- Drain in-flight RAG executions.
- Snapshot affected old points for every run, retain them for 14 days, and take
  a full Qdrant snapshot before the first production canary.
- Apply planned Qdrant replacement and manifest updates.
- Add idempotent retry and failure-state handling.
- Run structural verification.

**Gate:** simulated failures at each replacement step are recoverable, and no
query runs against a partially updated corpus.

### Phase 9C.5 — Full regression and scheduled operation

- Invoke the complete Phase 8 regression suite after structural verification.
- Associate regression results with the ingestion run and corpus version.
- Reopen the service only when all gates pass.
- Schedule updates for a configurable low-traffic window.
- Publish run duration, processed-message count, affected chunks, regression
  result, and maintenance duration.

**Gate:** repeated incremental batches pass structural checks and the complete
regression suite without quality degradation.

### Future Phase 9C.6 — Edits and deletions

- Capture update/delete events.
- Rebuild or remove affected chunks.
- Add historical mutation validation cases.

### Future Phase 9C.7 — Downtime gap workflow

- Detect unresumable gaps.
- Automatically create a review issue with recovery evidence.
- Add admin-provided missing-message bulk import.
- Run structural and full regression gates after recovery.

## 13. PR sequencing

PR #24 merged the reviewed design. Runtime implementation lands in focused
follow-up PRs. Phase 9C.2 is strictly an additive ownership-baseline change:
production Qdrant point mutation and pending-work consumption remain prohibited
until the later planner and replacement phases pass their own gates.
