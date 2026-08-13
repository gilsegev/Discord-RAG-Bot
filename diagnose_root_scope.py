"""Diagnostic (v4) - not part of the ingestion package.
Replicates _reply_aware_chunk's CURRENT (post v11.1 fix) grouping +
classification logic, without calling _build. Counts, per GROUP
rather than per message or per rendered-text-line, how many groups:
  a) qualify as a reply chunk with a scope-safe, resolved root
  b) qualify as a reply chunk but get scope-rejected (root -> None),
     including when the resolved metadata root can't be found at all
     (unresolved ancestor id from get_root_id(), matching
     chunk_manifest.py's resolve_root())
  c) never meet min_msgs at all and get merged into time-window
     chunks (root is always None there, by design - unrelated to
     the scope-safety fix)

v1/v2 conflated (b) and (c), producing a misleading 6.7% "null rate".
v3 fixed that conflation but used get_root_id() for GROUPING, which
was the Finding-1 bug: get_root_id() may return an unresolved
ancestor id, causing 225 messages corpus-wide to be mis-grouped
compared to the grouping key chunker.py actually uses (see
diagnose_grouping_diff.py for the full A/B message-level diff).

v4 (this version) uses the SAME two-stage resolution chunker.py uses
after the v11.1 fix:
  - GROUPING key: the resilient walk (_resolve_grouping_root_id
    equivalent below) - never returns an unresolved id.
  - root_message_id METADATA: resolved further via get_root_id() from
    that grouping anchor, checked for scope-safety against THAT value.
This is the accurate current baseline; supersedes the 1.10% figure
in chunker.py's module docstring, computed under v3's flawed grouping.
"""
from ingestion.parser import parse_all_exports
from ingestion.chunker import get_root_id, MIN_MSGS, MIN_MSGS_THREAD

records = parse_all_exports("chat_logs")
id_to_msg = {r["id"]: r for r in records}

by_channel = {}
for r in records:
    group_key = (r["channel"], r.get("thread_name"))
    by_channel.setdefault(group_key, []).append(r)


def resolve_grouping_root_id(msg, id_to_msg):
    """Mirrors chunker.py's _resolve_grouping_root_id() exactly -
    resilient walk, stops at last known message on any gap."""
    visited = set()
    current = msg
    while current.get("parent_id"):
        pid = current["parent_id"]
        if pid in visited or pid not in id_to_msg:
            break
        visited.add(pid)
        current = id_to_msg[pid]
    return current["id"]


safe_root   = 0
unsafe_root = 0
too_small   = 0

for (channel, thread_name), msgs in by_channel.items():
    is_thread = thread_name is not None
    min_msgs  = MIN_MSGS_THREAD if is_thread else MIN_MSGS

    group_channel_id  = msgs[0].get("channel_id") if msgs else None
    group_thread_name = msgs[0].get("thread_name") if msgs else None

    root_groups = {}
    assigned    = set()

    for msg in msgs:
        if msg.get("parent_id") and msg["parent_id"] in id_to_msg:
            # GROUPING key - same as chunker.py's
            # _resolve_grouping_root_id, always resolvable
            root_id = resolve_grouping_root_id(msg, id_to_msg)
            root_groups.setdefault(root_id, []).append(msg)
            assigned.add(msg["id"])
            if root_id in id_to_msg and root_id not in assigned:
                root_groups[root_id].insert(0, id_to_msg[root_id])
                assigned.add(root_id)

    for root_id, group_msgs in root_groups.items():
        # With the resilient grouping walk, root_id is always a real
        # message - this branch is now dead code in chunker.py itself,
        # kept here only for structural parity / safety.
        if root_id not in id_to_msg and \
                not any(m["id"] == root_id for m in group_msgs):
            continue

        if len(group_msgs) < min_msgs:
            too_small += 1
            continue

        # METADATA root - resolved further from the grouping anchor,
        # matching chunker.py's Edit 3 exactly.
        group_root_record = id_to_msg.get(root_id)
        resolved_root_id = (
            get_root_id(group_root_record, id_to_msg)
            if group_root_record else None
        )
        root_record = (
            id_to_msg.get(resolved_root_id) if resolved_root_id else None
        )
        scope_is_safe = bool(root_record) and (
            root_record.get("channel_id") == group_channel_id and
            root_record.get("thread_name") == group_thread_name
        )
        if scope_is_safe:
            safe_root += 1
        else:
            unsafe_root += 1

total_groups = safe_root + unsafe_root + too_small
print(f"Total reply-chain groups examined: {total_groups}")
print(f"  Safe root (kept):                 {safe_root}")
print(f"  Scope-rejected or unresolved root: {unsafe_root}")
print(f"  Below min_msgs (orphaned,")
print(f"    always None - unrelated to fix): {too_small}")
if safe_root + unsafe_root:
    pct = unsafe_root / (safe_root + unsafe_root) * 100
    print(f"\nTrue scope-rejection rate "
          f"(of qualifying reply chunks only): {pct:.2f}%")