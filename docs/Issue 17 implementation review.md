# Issue 17 implementation review

## Outcome

PR #50 now uses stable Discord `channel_id` as the only chunk scope and one
fail-closed reply-root resolver across full ingestion, ownership generation,
and incremental planning. No remaining implementation deviations were found.

## Review fixes

- Replaced display-name grouping and all duplicate ancestry walkers.
- Corrected live incremental normalization to preserve a Discord thread's own
  `channel_id` instead of substituting its parent channel.
- Removed thread display names and the redundant thread ID from ancestry and
  ownership decisions; legacy nullable database columns remain compatible.
- Added late-ancestor repair for prior null-root fallback ownership.
- Cached reply membership by channel/root for incremental planning.
- Bound replacement plans to chunker version `v11`.
- Kept message-overlap dedupe authoritative when roots are null or when a
  different-root candidate has the stronger overlap.
- Removed the one-off corpus diagnostic from production source.

The repository does not contain
`docs/04_Coding_Agent_Rules/engineering_insights.md`; the review therefore used
the repository `AGENTS.md` rules supplied to this task. Those rules were checked
for scope, ownership, layering, obsolete code, verification, and documentation;
no unresolved violation remains.

## Verified corpus comparison

| Measure | Current main | PR #50 |
|---|---:|---:|
| Parsed messages | 77,555 | 77,555 |
| Reply messages | 28,728 | 28,728 |
| Chunks | 32,756 | 32,840 |
| Message memberships | 115,786 | 116,168 |
| Unique represented messages | 77,555 | 77,555 |
| Dropped messages | 0 | 0 |
| Duplicate memberships | 38,231 | 38,613 |
| Mixed-channel chunks | 430 | 0 |
| Mixed-channel memberships | 1,504 | 0 |
| Trusted-root chunks | 0 | 13,365 |

Reply resolution produced 27,911 trusted roots, 592 missing immediate parents,
225 missing higher ancestors, zero cycles, and zero cross-channel references.

## As-built diagrams

### System

```text
Discord exports/capture
          |
          v
 shared root resolver -> chunker -> embedder -> Qdrant
          |                              |
          +-> manifest/incremental       v
                                   n8n root+overlap dedupe
```

### Components

```text
reply_roots: decide trusted ancestry
      |              |              |
      v              v              v
chunker        chunk manifest   incremental planner
      |                              |
      +---------- Qdrant payload ----+
```

### Code

```text
resolve_reply_root(message, index, channel)
  -> root | missing-parent | cycle | cross-channel
  -> chunk_records -> _build -> _split_if_needed
  -> point_to_manifest
  -> coalesce_work -> create_shadow_plan
```

### Workflow

```text
retrieve -> rerank -> strongest-first candidates
                         |
                         v
             trusted same-root overlap?
                  yes /       \ no
              root rule     message-overlap fallback
                         |
                         v
                  assemble context
```
