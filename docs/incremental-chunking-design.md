# Incremental Chunking Design
**Author:** Hemanth Aragonda (@haragonda)
**Status:** Draft for team review — v3
**Requested by:** Community team
**Date:** 23 June 2026
**Reviewed by:** Principal Engineer (23 June 2026)
**Related:** ingestion/chunker.py · ingestion/run.py · docs/retrieval-context-prompt-contracts.md · Phase 6 dedupe design

---

## Problem Statement

Full corpus rebuilds are not sustainable as the TPM Unite corpus grows. The current pipeline re-chunks and re-embeds every message on every run:

```
Current flow (every run):
Load all 22 chat log files
→ Parse all 25,003 messages
→ Chunk all messages (9,521 chunks)
→ Embed all chunks
→ Recreate Qdrant collection from scratch
→ 157 minutes per full rebuild
```

Rebuild time scales linearly with corpus size. As Discord exports grow — new channels, new exports, ongoing community activity — full rebuilds become operationally blocking.

---

## Current Readiness Assessment

This design is not fully implementation-ready on current `main`. The table below identifies what is complete, what is incomplete, and what must exist before each phase can begin.

| Area | What exists today | What is missing | Blocks |
|---|---|---|---|
| Full rebuild path | `ingestion/run.py` — full recreate mode working | Nothing — this path stays as-is | Nothing |
| Chunker algorithm | `ingestion/chunker.py` v10 — reply-aware, per-piece metadata, regression tests passing | `get_new_messages()` function for watermark split | Phase 3 |
| Qdrant state | 9,521 points, stable SHA-256 point IDs, correct per-piece `message_ids` (PR #22 merged) | No per-file ingestion state recorded anywhere | Phase 1 |
| Postgres schema | 6 RAG tables in `ragbot` DB — `rag_transactions`, `rag_retrieval_results`, etc. | `rag_ingestion_state` table does not exist | Phase 1 |
| Observability | Phoenix OTLP tracing live for active-call path | No ingestion-specific Phoenix spans exist | Phase 3 |
| Incremental run mode | `--incremental` flag exists in `run.py` | Currently falls back to full rebuild — no skip logic | Phase 2 |
| Contributor coordination | Single OCI instance, gilsegev manages deployment | No shared state — each contributor's local run is invisible to OCI | Phase 1 |

**Dependency sequence:** Phase 1 (`rag_ingestion_state` table) → Phase 2 (fingerprint skip) → Phase 3 (watermarking) → Phase 4 (orphan detection) → Phase 9 (passive listener unblocked).

Phase 1 can begin immediately after this doc is approved. No other PRs need to merge first.

---

## Goals

1. Process only genuinely new content on incremental runs
2. Preserve chunking quality — reply-aware grouping, time-window anchoring, 2-message overlap — for new content
3. Handle boundary cases: new messages arriving in existing time windows, new replies to old conversations
4. Keep Qdrant point IDs stable — no silent vector replacement without explicit intent
5. Keep the full rebuild path available for corpus corrections and emergency resets
6. Ensure state is consistent across all contributors running on the shared OCI instance

---

## Non-Goals

- Real-time streaming ingestion (out of scope for this design — see Alternatives Considered)
- Retroactive rechunking of historical chunks when new messages arrive mid-window
- Changing the chunking algorithm itself

---

## Root Cause: Why Naive Append Fails

The chunker is stateful in three ways:

**1. Time-window anchoring**
Chunks are built around conversation time windows. A new message with timestamp `T` may fall inside an existing window `[T-delta, T+delta]`, invalidating the existing chunk boundary.

**2. Reply-aware grouping**
A new reply to an old root message belongs in the same chunk as its parent. Naive append creates an orphaned reply chunk with no context.

**3. 2-message overlap**
The last 2 messages of chunk N become the first 2 messages of chunk N+1. Adding messages after chunk N was created invalidates this overlap.

**Conclusion:** True mid-window incremental chunking requires retroactive rechunking of affected windows — which effectively recreates the original problem. The design must avoid this.

---

## Proposed Design: Channel-Level Incremental Chunking

### Core Insight

The problem is not that incremental chunking is impossible — it is that the unit of incremental processing needs to be the **channel export file**, not the individual message.

Discord exports are always complete channel snapshots. Each export file contains the full history of that channel up to the export date. This gives us a clean, stable unit of deduplication.

### Mechanism: Export-Level Fingerprinting

Track which export files have been processed. On each run:
1. Compute a fingerprint (SHA-256) of each export file
2. Compare against stored fingerprints in `rag_ingestion_state` (Postgres)
3. Process only files whose fingerprint has changed or which are new
4. Append new chunks to Qdrant without touching existing points

```
Incremental flow:
Load export file list
→ Compare fingerprints against rag_ingestion_state (Postgres)
→ For changed/new files only:
    Parse messages
    → Chunk (reply-aware, time-window, overlap)
    → Embed
    → Upsert to Qdrant (new point IDs only)
    → Commit state row to Postgres on successful completion
→ ~1.2 minutes for a single new export
```

---

## State Storage: Postgres `rag_ingestion_state` Table

**Decision: State is stored in the `ragbot` Postgres database, not a local JSON file.**

A JSON state file in the repo was considered and rejected. In a multi-contributor environment where gilsegev runs ingestion on OCI and contributors run locally, a local file diverges silently — each contributor writes their own state, and the next OCI run starts from stale watermarks, causing silent re-ingestion of already-processed chunks. Qdrant upsert would handle duplicate point IDs correctly, but watermarks would be wrong and observability would be blind to the divergence.

Postgres on OCI is the single source of truth for all shared state in this project. Ingestion state belongs there.

```sql
-- Migration: deploy/phase0/sql/06-incremental-ingestion-state.sql

CREATE TABLE rag_ingestion_state (
    file_name           TEXT PRIMARY KEY,
    sha256              TEXT NOT NULL,
    message_watermark   TEXT NOT NULL,        -- highest message ID processed
    chunk_count         INTEGER NOT NULL,
    message_count       INTEGER NOT NULL,
    processed_at        TIMESTAMPTZ DEFAULT NOW(),
    run_id              TEXT NOT NULL,        -- ties rows to a single run
    rechunked_point_ids TEXT[] DEFAULT '{}'   -- point IDs rechunked in this run
);

CREATE INDEX idx_ingestion_state_run_id ON rag_ingestion_state(run_id);
CREATE INDEX idx_ingestion_state_processed_at ON rag_ingestion_state(processed_at);
```

**Idempotency guarantee:** State rows are written inside a transaction that also marks the run as complete. If `run.py` crashes mid-run, no partial state rows are committed — the next run starts from the last clean state. State is written per-file on successful completion of that file, never on partial completion.

**Access control:** `rag_ingestion_state` is accessible to `ragbot_admin` only. It is not exposed through any API surface. Point IDs in `rechunked_point_ids` are internal identifiers and must not be surfaced in public-facing responses.

---

## Boundary Window: Specification

**`OVERLAP_SIZE` is defined as `max(OVERLAP_MESSAGES, reply_chain_depth_at_boundary)`**
where `OVERLAP_MESSAGES = 2` (the chunker's existing overlap constant).

In practice, `OVERLAP_SIZE = 2` for standard conversation boundaries. For reply chains that span the watermark boundary, the window expands to include the full reply chain root — bounded at 10 messages to prevent pathological cases.

### Worked Example

Scenario: `#interview-experience` re-exported. Watermark = message ID `900000000000000000`. Channel has 6,935 messages. New export adds 47 messages after the watermark.

```
Messages before watermark (already indexed):
... msg_id 899999999999999998  [chunk N-1, normal message]
    msg_id 899999999999999999  [chunk N, boundary — overlap message 1]
    msg_id 900000000000000000  [chunk N, boundary — overlap message 2 = watermark]

Messages after watermark (new):
    msg_id 900000000000000001  [new message 1]
    msg_id 900000000000000002  [new message 2 — reply to msg 899999999999999999]
    ...
    msg_id 900000000000000047  [new message 47]

Boundary window = [msg 899999999999999999, msg 900000000000000000]
                  (last OVERLAP_MESSAGES=2 messages before watermark)

Rechunk scope:
  boundary window (2 messages) + new messages (47 messages) = 49 messages rechunked
  Historical chunks (6,933 messages) = untouched
```

**Pre/post conditions for `get_new_messages()`:**

```python
def get_new_messages(
    messages: list[dict],
    watermark: str,
    overlap_messages: int = OVERLAP_MESSAGES,
    max_boundary: int = 10
) -> tuple[list, list]:
    """
    Splits messages at the watermark into boundary and new messages.

    Pre-conditions:
        - messages is sorted ascending by message ID
        - watermark is a valid Discord message ID string
        - all message IDs are parseable as integers

    Post-conditions:
        - boundary contains the last min(overlap_messages, len(before)) messages
          before or at the watermark, expanded to include full reply chain root
          if a reply chain spans the boundary, up to max_boundary messages
        - new_messages contains all messages with ID > watermark
        - boundary + new_messages contains no duplicates
        - len(boundary) >= min(overlap_messages, len(before))

    Returns:
        boundary: overlap messages for context continuity
        new_messages: genuinely new messages to chunk and embed
    """
    watermark_int = int(watermark)
    before = [m for m in messages if int(m['id']) <= watermark_int]
    after = [m for m in messages if int(m['id']) > watermark_int]

    # Expand boundary if reply chain root is outside the default overlap window
    boundary = before[-overlap_messages:] if len(before) >= overlap_messages else before
    if after:
        first_new_parent = after[0].get('parent_id')
        if first_new_parent:
            parent_in_boundary = any(m['id'] == first_new_parent for m in boundary)
            if not parent_in_boundary:
                # Expand boundary to include the reply chain root, up to max_boundary
                boundary = before[-max_boundary:] if len(before) >= max_boundary else before

    return boundary, after
```

---

## Handling the Hard Cases

### Case 1: New export of an existing channel

Discord exports are full snapshots. A new export of `#interview-experience` will contain all historical messages plus new ones.

**Problem:** Naive fingerprint comparison marks the whole file as changed. Full rechunk of 6,935 messages.

**Solution: Message-ID watermarking**

Within a changed file, track the highest message ID already processed (`message_watermark` in `rag_ingestion_state`). On re-ingestion of a changed file:

1. Parse all messages
2. Split at the watermark using `get_new_messages()` — see Boundary Window section
3. Rechunk only the boundary window + new messages
4. Upsert rechunked boundary chunks with stable point IDs
5. Upsert new chunks with new point IDs
6. Log rechunked point IDs to `rag_ingestion_state.rechunked_point_ids`

**Result:** Only boundary window + new messages rechunked. Historical chunks untouched.

### Case 2: New reply to an old root message

A new Discord export includes a reply to a conversation from 6 months ago.

**Problem:** The reply belongs in the same chunk as its parent — but that chunk already exists in Qdrant.

**Solution: Reply orphan detection + targeted rechunk**

**Note: Phase 4 is a hard prerequisite for Phase 9 (passive listener).** Passive listener will generate new replies to old messages at higher frequency than batch exports. This is not deferred — it is scoped to Phase 4.

During boundary processing, detect replies whose `parent_id` points to a message in an already-processed chunk:

1. Look up `parent_id` in `rag_ingestion_state` → find the owning chunk's point ID
2. Flag that chunk for rechunking
3. Rechunk the parent chunk with the new reply included
4. Upsert with the same point ID (stable ID = no downstream dedupe breakage)
5. Log rechunked point ID to `rag_ingestion_state.rechunked_point_ids`
6. Emit Phoenix span: `ingestion.reply_orphan_rechunk` with `parent_point_id`, `reply_message_id`, `rechunked_at`

**Scope: direct parent only.** A reply's direct parent chunk is rechunked. Grandparent chains (reply to a reply to a reply) are out of scope for Phase 4. This covers the vast majority of Discord reply patterns.

This is bounded: a single reply triggers rechunking of at most one existing chunk.

### Case 3: New channel export (never seen before)

Straightforward — full chunk + embed + upsert. No existing state to reconcile. Commit state row to Postgres on successful completion.

### Case 4: Corrupted or partial export

If a file's SHA-256 changes but message count drops by more than `min(1%, 50 messages)` — whichever is smaller — reject the file and log a warning. Do not process partial exports.

```python
MAX_ACCEPTABLE_DROP_PCT = 0.01   # 1%
MAX_ACCEPTABLE_DROP_ABS = 50     # absolute floor

def is_suspected_truncation(
    new_count: int,
    stored_count: int
) -> bool:
    """
    Returns True if the message count drop exceeds the acceptable threshold.
    Uses the more conservative of percentage and absolute floor.

    Example: tc-sharing has 10,640 messages.
        1% threshold = 106 messages
        Absolute floor = 50 messages
        → reject if drop > 50 messages (absolute floor is more conservative)
    """
    drop = stored_count - new_count
    pct_threshold = stored_count * MAX_ACCEPTABLE_DROP_PCT
    return drop > min(pct_threshold, MAX_ACCEPTABLE_DROP_ABS)

if is_suspected_truncation(new_message_count, stored_message_count):
    log.warning(
        f"Suspected truncated export: {filename}. "
        f"Expected >= {stored_message_count - MAX_ACCEPTABLE_DROP_ABS}, "
        f"got {new_message_count}. Skipping — run continues."
    )
    continue  # skip this file, do not abort entire ingestion run
```

---

## Qdrant Point ID Stability

Current point IDs are SHA-256 hashes of `channel_id + first_message_id`. This is stable by design — the same chunk always produces the same point ID.

For rechunked boundary windows:
- If chunk content is unchanged → same point ID → Qdrant upsert is a no-op
- If chunk content changed (new messages added to window) → same point ID → Qdrant upsert updates the vector and payload atomically at the point level

**Upsert atomicity:** Qdrant guarantees atomic upserts at the individual point level. A query running concurrently with a boundary rechunk upsert will receive either the old vector+payload or the new vector+payload — never a mixed state. Reference: [Qdrant upsert documentation](https://qdrant.tech/documentation/concepts/points/#upsert-points).

**Silent quality change — observability required:** Adding messages to an existing chunk changes its embedding vector. A chunk that scored highly for a given query before rechunking may score differently after. This is expected and correct behaviour — the chunk now represents more context — but it must be observable.

Every rechunked point ID is logged to `rag_ingestion_state.rechunked_point_ids`. The following Phoenix span is emitted for each boundary rechunk event:

```
ingestion.boundary_rechunk
  file_name: "TPMs unite - ..."
  rechunked_point_count: 3
  rechunked_point_ids: ["abc123", "def456", "ghi789"]
  watermark: "900000000000000000"
  new_message_count: 47
  run_id: "run_20260623_174600"
  rechunked_at: "2026-06-23T17:46:00Z"
```

**No downstream dedupe breakage:** Phase 6 dedupe uses `message_ids` overlap, not point ID comparison (point ID is only used as a stable tiebreaker). Stable point IDs preserve dedupe correctness across rechunking events.

---

## Verify Flag Specification

`python run.py --verify` cross-checks `rag_ingestion_state` against the live Qdrant collection.

**What it checks:**
1. For every `file_name` in `rag_ingestion_state`, verify that all expected point IDs exist in Qdrant
2. For every point ID in Qdrant, verify that a corresponding `rag_ingestion_state` row exists
3. Compare `chunk_count` in state against actual Qdrant point count per channel
4. Identify any point IDs in `rechunked_point_ids` that no longer exist in Qdrant (orphaned rechunk records)

**Output:**
```
=== Ingestion State Verification ===
Files in state:          22
Files verified OK:       21
Files with discrepancy:   1
  ⚠ interview-experience: state=2847 chunks, qdrant=2844 chunks (delta: -3)

Qdrant points without state record:   0
State records without Qdrant points:  3  ← likely from a failed partial run

Orphaned rechunk records:             0

Verdict: WARN — state divergence detected. Run with --force-rebuild to reset.
```

**On divergence:** `--verify` reports only. It does not auto-repair. Auto-repair risks compounding a divergence. Repair requires either a targeted re-run of the affected file or a full `--force-rebuild`.

**Trigger conditions:** Run `--verify` manually after any failed run, before a major corpus expansion, and as a monthly health check.

---

## Alternatives Considered

### Alternative 1: Real-time streaming ingestion (Discord webhook → chunker → Qdrant)

Each new Discord message triggers a chunker run and immediate embedding.

**Rejected because:** Reply-aware chunking requires seeing the full conversation context before chunking. A real-time single-message trigger would produce single-message chunks with no reply context, degrading retrieval quality significantly. Additionally, Discord's rate limits on webhook delivery and the CPU cost of an embedding call per message make this operationally expensive for a volunteer-run OCI instance. Deferred as a separate design when the community is ready to invest in a real-time path.

### Alternative 2: Hash-ring channel partitioning

Assign channels to partitions by hash. Each partition runs independently with its own state.

**Rejected because:** Reply chains cross channel boundaries in TPM Unite's forum-discussion structure. A reply in a thread could belong to a different partition than its parent, breaking reply-aware grouping. Complexity cost is high for a team of 4 volunteers. Export-file granularity achieves the same incremental benefit without partition management overhead.

### Alternative 3: Chunk-level content hashing (skip unchanged chunks)

Hash the text content of each chunk. Skip embedding for chunks whose hash hasn't changed.

**Considered as a Phase 3 enhancement.** Not adopted as the primary mechanism because it requires parsing and chunking the full file before knowing what to skip — the expensive step is the embedding, not the chunking. Watermarking skips the chunking step entirely for historical messages. Chunk-level hashing could be layered on top of watermarking as a secondary optimisation in Phase 3 if embedding cost becomes the bottleneck.

---

## Output Contract

A successful incremental run produces the following. Each field is required — absence of any field indicates an incomplete or failed run.

### Per-file record written to `rag_ingestion_state`

| Field | Type | Meaning | Example |
|---|---|---|---|
| `file_name` | TEXT | Export filename as it appears on disk | `TPMs unite - interview-experience [879539].json` |
| `sha256` | TEXT | SHA-256 of the export file at time of processing | `a3f9c2d1...` |
| `message_watermark` | TEXT | Highest Discord message ID processed in this run | `900000000000000047` |
| `chunk_count` | INTEGER | Total Qdrant points owned by this file after this run | `2894` |
| `message_count` | INTEGER | Total messages parsed from this file | `6982` |
| `processed_at` | TIMESTAMPTZ | Wall-clock time the file completed processing | `2026-06-23T17:46:00Z` |
| `run_id` | TEXT | Unique ID for the ingestion run — all files in one run share this ID | `run_20260623_174600` |
| `rechunked_point_ids` | TEXT[] | Point IDs upserted due to boundary rechunk or orphan detection | `{abc123, def456}` |

### Per-run stdout summary (run.py terminal output)

```
=== Incremental Ingestion Run ===
Run ID:              run_20260623_174600
Mode:                incremental

Files evaluated:     22
Files skipped:       21  (fingerprint unchanged)
Files processed:      1  (fingerprint changed or new)

  interview-experience:
    Messages parsed:    6,982  (was 6,935 — 47 new)
    Boundary rechunked: 2 points
    New chunks added:   47 points
    Rechunk IDs:        [abc123, def456]
    Watermark updated:  900000000000000047

Qdrant points total: 9,568  (was 9,521 — net +47)
State rows updated:      1
Run duration:         4m 12s

Phoenix spans emitted:
  ingestion.boundary_rechunk × 1
  ingestion.file_skipped × 21
  ingestion.file_processed × 1
```

### Phoenix spans emitted per run

| Span name | Emitted when | Key fields |
|---|---|---|
| `ingestion.run_started` | Every run | `run_id`, `mode`, `file_count` |
| `ingestion.file_skipped` | Fingerprint unchanged | `file_name`, `sha256`, `reason` |
| `ingestion.file_processed` | File successfully ingested | `file_name`, `new_message_count`, `new_chunk_count`, `duration_ms` |
| `ingestion.boundary_rechunk` | Boundary window rechunked | `file_name`, `rechunked_point_count`, `rechunked_point_ids`, `watermark` |
| `ingestion.reply_orphan_rechunk` | New reply to old root (Phase 4) | `parent_point_id`, `reply_message_id`, `rechunked_at` |
| `ingestion.file_rejected` | Truncated or corrupt export | `file_name`, `stored_count`, `new_count`, `rejection_reason` |
| `ingestion.run_completed` | Run finished successfully | `run_id`, `files_processed`, `total_duration_ms`, `qdrant_point_delta` |
| `ingestion.run_failed` | Run failed mid-execution | `run_id`, `failed_at_file`, `error`, `state_rolled_back` |

---

## Implementation Plan

### Phase 1 — Postgres state tracking (no chunking changes)

Add `rag_ingestion_state` table to Postgres. `run.py` writes state rows on successful file completion. Full rebuild still used — but state is now recorded and consistent across contributors.

**Files changed:** `ingestion/run.py` · `deploy/phase0/sql/06-incremental-ingestion-state.sql`
**Deliverable:** State table written on each run. Idempotent — partial runs leave no partial state. `--verify` flag operational.
**Acceptance criteria:** After a full rebuild, `rag_ingestion_state` has 22 rows, one per export file, with correct `chunk_count` and `message_watermark`.

### Phase 2 — File-level skip (fingerprint deduplication)

Skip files whose SHA-256 matches `rag_ingestion_state`. Full rechunk still used for changed files.

**Files changed:** `ingestion/run.py`
**Deliverable:** Unchanged files skipped. Run time for no-change run drops to < 1 minute.
**Acceptance criteria:** A run with no new exports completes in < 1 minute. `rag_ingestion_state` is not modified for unchanged files.

### Phase 3 — Message-ID watermarking (boundary rechunk only)

For changed files, rechunk only the boundary window + new messages. Log rechunked point IDs. Emit Phoenix spans.

**Files changed:** `ingestion/chunker.py` · `ingestion/run.py`
**Deliverable:** Updated `tc-sharing` export (10,640 messages) processed in < 5 minutes.
**Acceptance criteria:** See performance targets table. Phoenix shows `ingestion.boundary_rechunk` span for each rechunked boundary.

### Phase 4 — Reply orphan detection (targeted parent rechunk)

**Hard prerequisite for Phase 9 (passive listener).** Detect new replies to old root messages and rechunk the parent chunk only. Emit `ingestion.reply_orphan_rechunk` Phoenix span.

**Files changed:** `ingestion/chunker.py` · `ingestion/run.py`
**Deliverable:** Reply chains stay coherent across incremental runs. Phase 9 passive listener unblocked.
**Acceptance criteria:** A new export containing a reply to a 6-month-old message correctly rechunks the parent chunk only. `rag_ingestion_state.rechunked_point_ids` contains the parent chunk's point ID. Phoenix shows the orphan rechunk span.

---

## Performance Targets

| Run type | Current | Target (Phase 2) | Target (Phase 3+) |
|---|---|---|---|
| Full rebuild (22 files) | 157 min | 157 min (unchanged) | 157 min (unchanged) |
| No new exports | 157 min | < 1 min | < 1 min |
| 1 new export file (small) | 157 min | 8–15 min | < 5 min |
| 1 existing export updated (small) | 157 min | 8–15 min | < 5 min |
| New reply to old message | 157 min | 8–15 min | < 2 min |
| **tc-sharing updated (10,640 messages)** | **157 min** | **8–15 min** | **< 5 min** |
| **interview-experience updated (6,935 messages)** | **157 min** | **8–15 min** | **< 5 min** |

---

---

## Validation Cases

The following cases must pass before any phase merges. Cases marked **[P1]**, **[P2]**, **[P3]**, **[P4]** indicate the earliest phase in which that case is testable.

| # | Case | Setup | Expected behaviour | Phase |
|---|---|---|---|---|
| 1 | No new exports | All 22 files unchanged — fingerprints match `rag_ingestion_state` | All 22 files skipped. `rag_ingestion_state` unchanged. Run completes in < 1 min. | P2 |
| 2 | Single new export (small channel) | Add new file not in `rag_ingestion_state` | File parsed, chunked, embedded, upserted. State row written. Other 21 files untouched. | P2 |
| 3 | Single existing export updated — tail append only | `#tpm-tradecraft` re-exported with 15 new messages at tail | Boundary window (2 msgs) + 15 new messages rechunked. 1,500+ historical messages untouched. State watermark updated. | P3 |
| 4 | Large channel updated — tc-sharing (10,640 messages) | tc-sharing re-exported with 200 new messages | Only boundary window + 200 messages rechunked. Run completes in < 5 min. 10,440 historical messages untouched. | P3 |
| 5 | New message is direct reply to old root | New export contains reply to message 6 months old, outside boundary window | Reply orphan detected. Parent chunk rechunked with reply included. Upserted with same point ID. `rechunked_point_ids` logged. | P4 |
| 6 | New reply — root already in boundary window | New export contains reply whose root is within the 2-message overlap boundary | No orphan detection needed. Boundary rechunk includes both root and reply naturally. | P3 |
| 7 | Run crashes mid-file | Simulate crash (KeyboardInterrupt) during embedding of file 3 of 5 | Files 1–2 state rows committed. File 3 state row not committed. Next run retries file 3 from scratch. No partial state in Postgres. | P1 |
| 8 | Truncated export — small channel | Re-export with message count 60 below stored count (exceeds 50-message floor) | File rejected. Warning logged. Run continues with remaining files. State unchanged for this file. | P2 |
| 9 | Truncated export — large channel | tc-sharing re-exported with 80 messages missing (< 1% of 10,640) | File accepted — drop is within `min(1%, 50)` threshold. **Note:** 1% of 10,640 = 106 > 50, so absolute floor of 50 applies. File rejected if drop > 50. | P2 |
| 10 | Multiple contributors run simultaneously | gilsegev runs on OCI while contributor runs locally against same Postgres | Postgres row-level locking prevents concurrent writes to same `file_name`. Second writer waits or fails cleanly — no silent state divergence. | P1 |
| 11 | `--verify` on clean state | Run `--verify` after a successful full rebuild | All 22 files verified. 0 discrepancies. 0 orphaned records. Verdict: OK. | P1 |
| 12 | `--verify` on diverged state | Manually delete 3 Qdrant points without updating state | `--verify` reports 3 state records without Qdrant points. Verdict: WARN. Does not auto-repair. | P1 |
| 13 | `--force-rebuild` clears and resets | Run `--force-rebuild` after state divergence | Full rebuild runs. All 22 files re-processed. `rag_ingestion_state` truncated and rewritten. Qdrant collection recreated. | P1 |
| 14 | Boundary falls mid-reply-chain | Watermark splits a reply chain — root before watermark, 2 replies after | Boundary window expands beyond 2 messages to include reply chain root (up to `max_boundary=10`). All 3 messages in same chunk after rechunk. | P3 |

---

## Merge Gate

Each phase has a hard merge gate. A PR must not merge until every item in its gate is satisfied. Partial satisfaction is not accepted.

### Phase 1 Merge Gate
- [ ] `deploy/phase0/sql/06-incremental-ingestion-state.sql` exists and applies cleanly to the running OCI ragbot DB
- [ ] After a full rebuild, `rag_ingestion_state` contains exactly 22 rows — one per export file
- [ ] Each row has a non-null `sha256`, `message_watermark`, `chunk_count`, `message_count`, and `run_id`
- [ ] A simulated mid-run crash (KeyboardInterrupt after file 3) leaves no partial state rows in Postgres (Validation Case 7)
- [ ] `python run.py --verify` returns `Verdict: OK` on a clean state (Validation Case 11)
- [ ] `python run.py --verify` returns `Verdict: WARN` when 3 Qdrant points are manually deleted (Validation Case 12)
- [ ] `python run.py --force-rebuild` completes successfully and rewrites all 22 state rows (Validation Case 13)
- [ ] `rag_ingestion_state` is not accessible to any user other than `ragbot_admin`

### Phase 2 Merge Gate
- [ ] A run with all 22 files unchanged completes in < 1 minute (Validation Case 1)
- [ ] `rag_ingestion_state` is not modified for skipped files
- [ ] Phoenix emits `ingestion.file_skipped` span for each skipped file
- [ ] A truncated export (message count drop > 50) is rejected, logged, and the run continues — not aborted (Validation Case 8)
- [ ] A new export file (never seen) is fully processed and its state row is written (Validation Case 2)
- [ ] All Phase 1 merge gate conditions remain satisfied

### Phase 3 Merge Gate
- [ ] `get_new_messages()` passes unit tests for all boundary conditions: standard tail append, reply chain spanning boundary, fewer messages than `OVERLAP_SIZE`, empty `after` list
- [ ] `#tpm-tradecraft` re-exported with 15 new messages: only boundary window + 15 messages rechunked, 1,500+ historical messages untouched (Validation Case 3)
- [ ] tc-sharing re-exported with 200 new messages: run completes in < 5 minutes (Validation Case 4)
- [ ] Boundary window spanning a reply chain expands correctly up to `max_boundary=10` (Validation Case 14)
- [ ] Phoenix emits `ingestion.boundary_rechunk` span with `rechunked_point_ids`, `watermark`, `new_message_count`
- [ ] `rag_ingestion_state.rechunked_point_ids` is populated for every rechunked boundary
- [ ] Qdrant point count after incremental run equals: prior count + new_chunk_count only (rechunked points replace, not duplicate)
- [ ] All Phase 1 and Phase 2 merge gate conditions remain satisfied

### Phase 4 Merge Gate
- [ ] A new export containing a reply to a message 6 months old correctly rechunks the parent chunk only — not the full channel (Validation Case 5)
- [ ] `rag_ingestion_state.rechunked_point_ids` contains the parent chunk's point ID
- [ ] Phoenix emits `ingestion.reply_orphan_rechunk` span with `parent_point_id`, `reply_message_id`, `rechunked_at`
- [ ] Reply whose root is within the boundary window is handled by boundary rechunk, not orphan detection — no double-rechunk (Validation Case 6)
- [ ] Grandparent chain (reply to a reply) does not trigger cascading rechunks — only direct parent is rechunked
- [ ] Phase 9 (passive listener) team confirms this implementation satisfies their reply coherence requirement before Phase 4 merges
- [ ] All Phase 1, 2, and 3 merge gate conditions remain satisfied

---

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Multi-contributor state divergence | ~~Critical~~ **Resolved** | Postgres state table with transaction-backed writes |
| Boundary window rechunk creates duplicate message_ids | Medium | Stable point IDs ensure upsert replaces, not duplicates |
| Rechunked vector silently changes retrieval ranking | Medium | `rechunked_point_ids` logged to Postgres + Phoenix span emitted |
| Large channel export updated (tc-sharing) | Low | Watermarking limits rechunk to boundary + suffix regardless of file size |
| Reply orphan detection misses grandparent chains | Low | Phase 4 scope: direct parent only. Grandparent chains deferred post-Phase 9 assessment |
| Partial run leaves orphaned Qdrant points | Low | `--verify` flag detects divergence; `--force-rebuild` corrects |
| Truncated export silently accepted | ~~Medium~~ **Resolved** | `min(1%, 50 messages)` floor — conservative absolute threshold |

---

## Open Questions

The following questions remain open for team decision. Phase 2 implementation can begin without resolving them — they affect Phase 3 and beyond.

1. **Watermark granularity** — per-file or per-channel? Per-channel is more precise when a channel has multiple exports but requires mapping each export file to a channel ID. Current proposal uses per-file. Team input requested before Phase 3.

2. **Full rebuild trigger conditions** — proposed: `--force-rebuild` flag (manual) OR automatic trigger when > 20% of files have changed fingerprints (corpus restructure signal). Team to confirm threshold.

3. **Phase 4 timing** — Phase 4 is confirmed as a prerequisite for Phase 9. Does the team want to schedule Phase 4 immediately after Phase 3, or is there a gap workstream between them?

---

## What This Does Not Solve

This design addresses sustainable incremental ingestion for **batch Discord exports**. It does not address:

- **Real-time message ingestion** — new messages posted to Discord after an export require a new export to be processed. A streaming ingestion path (Discord webhook → chunker → Qdrant) would eliminate this gap but is a separate design — see Alternatives Considered.

- **Retroactive rechunking on algorithm change** — if the chunking algorithm itself changes (e.g. new overlap size, new window logic), a full rebuild is still required. The `--force-rebuild` flag handles this.

---

## Summary

The sustainable path is not to avoid rechunking — it is to bound it. By tracking export file fingerprints and message-ID watermarks in Postgres, incremental runs process only what has genuinely changed. Historical chunks are untouched. Qdrant point IDs remain stable. Phase 6 dedupe correctness is preserved. Rechunking events are fully observable via Postgres state records and Phoenix spans.

Full rebuild remains available and correct for corpus corrections. Incremental runs become the default operational path.

Phase 4 (reply orphan detection) is a hard prerequisite for Phase 9 (passive listener) and must be scoped and scheduled accordingly.