# Stable Channel-ID Grouping Implementation Review

Date: 2026-09-13

## Scope and outcome

The implementation changes full and incremental chunk scope from display names
or parent-channel/thread tuples to the originating Discord `channel_id`. It does
not rebuild production Qdrant or add the Issue #17 `root_message_id` payload
feature. The repository does not contain
`docs/04_Coding_Agent_Rules/engineering_insights.md`, so its numbered 20-clause
checklist was unavailable; the repository `AGENTS.md` rules were re-read and
applied instead.

## Deviation report

No unresolved deviations. Independent review found and the implementation fixed:

- cross-channel or missing-parent replies now fall back to the same-channel
  window path instead of becoming permanently deferred incremental groups;
- a v11 incremental plan cannot validate or persist against a v10 source corpus;
- live thread records retain the parent channel display name used by full exports;
- manifest reply ancestry stops at cross-channel boundaries, including cycles;
- point-ID documentation now matches complete membership plus split-index hashing;
- a new reply now replaces the old window point that owned its root; and
- bounded window reconstruction discards new overlap-only prefix artifacts.

`thread_name` still controls the established thread header and singleton policy.
That is intentionally preserved behavior; it is not a grouping identity.

## Rule and quality checks

- Scope, security, production-mutation, narrow-change, naming, dependency,
  verification, negative-test, and documentation rules: followed.
- New abstractions, external contracts, storage schema changes, concurrency,
  network fan-out, and production operations: not applicable.
- Unrecorded assumptions or open calls: none.

Three independent review passes covered adversarial correctness, architecture
and domain ownership, and efficiency/encapsulation. Confirmed findings were
fixed before the final verification run.

## As-built diagrams

### System

```text
Discord exports / durable capture
              |
              v
       stable-channel chunker
              |
       +------+------+
       v             v
 incremental plan   full rebuild (future)
       |             |
       v             v
 manifest/executor  Qdrant + manifest seed
```

### Components

```text
records
  -> group by channel_id
  -> resolve replies within channel_id
  -> reply chunks or time windows
  -> token split with existing overlap
  -> deterministic point IDs
```

### Changed code

```text
chunk_records
  -> per-channel id_to_msg
  -> _reply_aware_chunk

coalesce_work
  -> _root_id (channel bounded)
  -> channel reply/window groups
  -> _shadow_records (channel bounded)
```

### Runtime workflow

```text
load actual message channel_id
  -> reject work/source mismatch
  -> select same-channel affected records
  -> run shared chunker
  -> require source chunker version match
  -> validate plan
  -> executor remains unchanged
```
