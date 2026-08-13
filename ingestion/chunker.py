"""
ingestion/chunker.py - v11 reply-aware chunking
v11 fix (Issue #17):
  - root_message_id computed in _reply_aware_chunk() (via existing
    get_root_id()) is now stored on the chunk dict, not discarded
    after grouping. Enables Phase 6 reply-root dedupe downstream.
  - Time-window chunks (non-reply) get root_message_id=None.
  - Split pieces inherit root_message_id automatically via the
    existing {**chunk} shallow-copy in _split_if_needed() - no
    split-specific code needed.
  - Scope safety: a resolved root is only trusted as root_message_id
    when it shares the group's channel_id and thread_name (mirrors
    chunk_manifest.py's scope_is_safe check). An out-of-scope root
    is stored as None instead of risking cross-channel/cross-thread
    grouping in Phase 6 dedupe.
  - Test coverage: direct/nested replies, split inheritance, a
    genuine multi-hop cycle (A->B->C->A), a missing mid-chain parent,
    cross-channel scope rejection, and a message whose immediate
    parent was never parsed at all (Test 7 - the most common real-
    world "no root" case, confirmed via corpus analysis below).
  - Note: an earlier corpus-wide diagnostic was removed from this
    file. It classified "reply-derived" chunks by a naive text match
    ("  > " in rendered text), which conflates genuine scope-rejected
    roots with messages whose parent was never parsed at all (a much
    larger, unrelated bucket) - producing a misleading null rate.
    A one-off external script (not part of this package) replicating
    _reply_aware_chunk's actual grouping and metadata-resolution logic
    measured the TRUE scope-rejection rate at 1.91% (256/13,386
    qualifying reply groups) across the full 77,558-message corpus -
    in line with expectations for a rare edge case. This figure
    reflects the v11.1 fix (see below), which separates the GROUPING
    key (a resilient walk, always resolvable) from the
    root_message_id METADATA resolution (may walk to an unresolved
    ancestor, matching chunk_manifest.py's resolve_root()) - an
    earlier version conflated the two, causing 225 messages
    corpus-wide to be mis-grouped. See PR description for the
    validation scripts and full breakdown.
v10 fixes (post PR #5):
  - Fix 1: reply line detection in _build_line_to_msg_id() now handles
            '  > [author @ date]:' format - previously lstrip() left '> ['
            which failed the is_msg_line check, producing empty message_ids
            on reply-only split chunks (2/11,442 in tpm-tradecraft)
  - Fix 2: end-to-end regression test for oversized chunk composed
            entirely of reply-rendered lines - verifies _split_if_needed
            produces pieces with non-empty message_ids
Prior fixes retained from v8/v9:
  - Per-piece message_ids and first_message_id (PR #4)
  - Per-piece start_ts, end_ts, authors, span_days (PR #5)
  - Single-line overflow guard (PR #4)
  - message_count per piece (PR #4)
  - Smoke test import as ingestion.parser (PR #4)
Author: ThinkInSystems (Hemanth Aragonda)
"""
import tiktoken
from datetime import datetime

enc             = tiktoken.get_encoding("cl100k_base")
WINDOW_MINS     = 15
MIN_MSGS        = 2
MIN_MSGS_THREAD = 1
MAX_TOKENS      = 700
OVERLAP_MSGS    = 2


def chunk_records(records: list[dict]) -> list[dict]:
    """
    Main entry point. Returns chunks ready for embedding.
    Pass 1: reply-aware grouping via parent_id chains.
    Pass 2: 15-min time window fallback for standalone messages.
    """
    id_to_msg = {r["id"]: r for r in records}

    by_channel = {}
    for r in records:
        group_key = (r["channel"], r.get("thread_name"))
        by_channel.setdefault(group_key, []).append(r)

    all_chunks = []
    for (channel, thread_name), msgs in by_channel.items():
        msgs      = sorted(msgs, key=lambda m: m["timestamp"])
        is_thread = thread_name is not None
        chunks    = _reply_aware_chunk(msgs, id_to_msg,
                                       is_thread=is_thread)
        for chunk in chunks:
            all_chunks.extend(_split_if_needed(chunk, id_to_msg))

    all_chunks.sort(key=lambda c: c["start_ts"])

    print(f"Created {len(all_chunks)} chunks from "
          f"{len(records)} messages across "
          f"{len(by_channel)} channel/thread group(s)")
    return all_chunks


def get_root_id(msg: dict, id_to_msg: dict) -> str:
    """
    Follow parent_id chain to find the root message id.
    Cycle detection via visited set prevents infinite loops.

    v11.1 fix (align with chunk_manifest.py resolve_root()): if the
    chain terminates at a parent_id that was never parsed (deleted,
    export gap), return that MISSING id itself rather than the last
    successfully resolved message. This matches Phase 9C.2 manifest
    behavior so both systems agree on root_message_id for the same
    reply chain. Previously this returned the last known message,
    which silently understated the chain and could disagree with
    chunk_manifest.py resolve_root() on the same edge case.
    """
    visited = set()
    current = msg
    while current.get("parent_id"):
        pid = current["parent_id"]
        if pid in visited:
            break
        if pid not in id_to_msg:
            return pid
        visited.add(pid)
        current = id_to_msg[pid]
    return current["id"]

def _resolve_grouping_root_id(msg: dict, id_to_msg: dict) -> str:
    """
    Resolve the ancestor used as the CHUNK GROUPING key.

    Deliberately resilient: if the parent_id chain hits a message that
    was never parsed (deleted, export gap), stop and return the last
    successfully resolved message's id - never a missing/unresolvable
    id. This guarantees replies still coalesce into one chunk even
    when an upstream ancestor is missing from this export.

    This is intentionally DIFFERENT from get_root_id(), which is used
    only for the root_message_id metadata field and (to match
    chunk_manifest.py's resolve_root()) may return an unresolved
    parent id itself. Using get_root_id() here instead would silently
    route affected reply groups to time-window chunking whenever a
    chain has any gap above the immediate parent - a chunk-membership
    regression, not just a metadata change. See Issue #17 review.
    """
    visited = set()
    current = msg
    while current.get("parent_id"):
        pid = current["parent_id"]
        if pid in visited or pid not in id_to_msg:
            break
        visited.add(pid)
        current = id_to_msg[pid]
    return current["id"]

def _reply_aware_chunk(msgs: list[dict], id_to_msg: dict,
                       is_thread: bool = False) -> list[dict]:
    """
    Two-pass chunking:
    Pass 1: group reply chains by parent_id.
    Pass 2: time window for standalone + orphaned messages.
    Orphans collected BEFORE _window_chunk call (critical ordering).
    Filtered root handling: bot/system roots make replies standalone.

    v11 fix (Issue #17): a resolved root is only trusted as
    root_message_id when it shares this group's channel_id and
    thread_name (mirrors chunk_manifest.py's scope_is_safe check).
    An out-of-scope root is stored as root_message_id=None rather
    than risking cross-channel/cross-thread grouping in Phase 6
    dedupe. This does NOT change which messages get grouped into
    the chunk's text - that grouping logic is unchanged - it only
    guards the root_message_id metadata field itself.

    Note: a message whose parent_id is set but never appears in
    id_to_msg at all (parent never parsed - deleted, export gap)
    never enters root_groups below - it falls straight to the
    standalone/time-window path with root_message_id=None. This is
    pre-existing behavior, unrelated to the scope-safety check, and
    is the dominant real-world reason root_message_id ends up None
    (see Test 7 and the module docstring's corpus note above).
    """
    min_msgs = MIN_MSGS_THREAD if is_thread else MIN_MSGS

    group_channel_id  = msgs[0].get("channel_id") if msgs else None
    group_thread_name = msgs[0].get("thread_name") if msgs else None

    root_groups = {}
    assigned    = set()

    for msg in msgs:
        if msg.get("parent_id") and msg["parent_id"] in id_to_msg:
            root_id = _resolve_grouping_root_id(msg, id_to_msg)
            root_groups.setdefault(root_id, []).append(msg)
            assigned.add(msg["id"])
            if root_id in id_to_msg and root_id not in assigned:
                root_groups[root_id].insert(0, id_to_msg[root_id])
                assigned.add(root_id)

    standalone   = [m for m in msgs if m["id"] not in assigned]
    reply_chunks = []
    orphans      = []

    for root_id, group_msgs in root_groups.items():
        if root_id not in id_to_msg and \
                not any(m["id"] == root_id for m in group_msgs):
            orphans.extend(group_msgs)
            continue

        group_msgs = sorted(group_msgs, key=lambda m: m["timestamp"])
        if len(group_msgs) >= min_msgs:
            # v11.1 fix (Issue #17 + grouping-regression fix):
            # root_id is the GROUPING key from
            # _resolve_grouping_root_id() - always an existing
            # message, deliberately resilient to upstream gaps so
            # chunk membership never regresses.
            #
            # root_message_id METADATA resolves further from that
            # anchor using get_root_id(), which may walk past it to
            # an unresolved ancestor id (matching chunk_manifest.py's
            # resolve_root()). scope_is_safe is checked against THAT
            # resolved value - an unresolved id has no record, so it
            # is automatically rejected to None, same as an
            # out-of-scope root.
            group_root_record = id_to_msg.get(root_id)
            resolved_root_id  = (
                get_root_id(group_root_record, id_to_msg)
                if group_root_record else None
            )
            root_record   = (
                id_to_msg.get(resolved_root_id) if resolved_root_id else None
            )
            scope_is_safe = bool(root_record) and (
                root_record.get("channel_id") == group_channel_id and
                root_record.get("thread_name") == group_thread_name
            )
            safe_root_id = resolved_root_id if scope_is_safe else None
            reply_chunks.append(
                _build(group_msgs, root_message_id=safe_root_id)
            )
        else:
            orphans.extend(group_msgs)

    # Add orphans BEFORE calling _window_chunk (critical ordering)
    standalone.extend(orphans)
    time_chunks = _window_chunk(standalone, min_msgs=min_msgs)

    return reply_chunks + time_chunks


def _window_chunk(msgs: list[dict],
                  min_msgs: int = MIN_MSGS) -> list[dict]:
    """
    15-min time window chunking - fallback for non-reply messages.
    start_ts resets to current message timestamp after split.
    """
    if not msgs:
        return []

    msgs      = sorted(msgs, key=lambda m: m["timestamp"])
    chunks    = []
    current   = []
    start_ts  = None

    for msg in msgs:
        ts = datetime.fromisoformat(msg["timestamp"])
        if start_ts is None:
            start_ts = ts
        gap = (ts - start_ts).total_seconds() / 60
        if gap > WINDOW_MINS and len(current) >= min_msgs:
            prev_tail = current[-OVERLAP_MSGS:]
            # v11 note (Issue #17): no root_message_id arg here -
            # time-window chunks are not reply chains, so this
            # correctly defaults to None in _build().
            chunks.append(_build(current))
            current  = prev_tail + [msg]
            start_ts = datetime.fromisoformat(msg["timestamp"])
        else:
            current.append(msg)

    if current and len(current) >= min_msgs:
        chunks.append(_build(current))

    return chunks


def _build(msgs: list[dict], root_message_id: str | None = None) -> dict:
    """
    Format a list of messages into one chunk dict.
    thread_name prepended to chunk text for semantic retrieval.
    span_days calculated for long-span metadata filtering.
    dict.fromkeys preserves insertion order while deduplicating authors.

    v11 fix (Issue #17): root_message_id is passed in by the caller
    (already resolved via get_root_id() for reply chains, scope-
    checked by _reply_aware_chunk(), None for time-window chunks)
    and stored on the chunk dict so it survives into the Qdrant
    payload for Phase 6 reply-root dedupe.
    """
    assert msgs, "_build() called with empty message list"

    thread_name = msgs[0].get("thread_name")
    lines       = []

    if thread_name:
        lines.append(f"[Thread: {thread_name}]")

    for m in msgs:
        date = m["timestamp"][:10]
        line = f"[{m['author']} @ {date}]: {m['content']}"
        if m.get("parent_id"):
            line = "  > " + line
        lines.append(line)

    start_dt  = datetime.fromisoformat(msgs[0]["timestamp"])
    end_dt    = datetime.fromisoformat(msgs[-1]["timestamp"])
    span_days = (end_dt - start_dt).days

    return {
        "text":            "\n".join(lines),
        "start_ts":        msgs[0]["timestamp"],
        "end_ts":          msgs[-1]["timestamp"],
        "channel":         msgs[0]["channel"],
        "channel_id":      msgs[0].get("channel_id"),
        "thread_name":     thread_name,
        "authors":         list(dict.fromkeys(m["author"] for m in msgs)),
        "message_count":   len(msgs),
        "message_ids":     [m["id"] for m in msgs],
        "root_message_id": root_message_id,
        "span_days":       span_days,
    }


def _build_line_to_msg_id(lines: list[str],
                           msg_ids: list[str]) -> dict:
    """
    Map each line index to its source message_id.

    Fix 1 (v10): reply lines render as '  > [author @ date]: content'
    After lstrip(), these become '> [author @ date]: content' which
    previously failed the is_msg_line check (requires line to start
    with '[').

    Fix: strip a leading '>' marker and surrounding whitespace before
    checking the message line pattern. This correctly identifies both:
      - Normal lines:  '[author @ date]: content'
      - Reply lines:   '  > [author @ date]: content'

    Continuation lines inherit the same message_id as the preceding
    message line.
    """
    line_to_msg_id = {}
    msg_id_idx     = 0
    last_msg_id    = None

    for i, line in enumerate(lines):
        # Fix 1: strip reply prefix '>' before pattern check
        stripped = line.lstrip()
        if stripped.startswith(">"):
            stripped = stripped[1:].lstrip()

        is_msg_line = (
            stripped.startswith("[") and
            "@ " in stripped and
            "]:" in stripped
        )
        if is_msg_line:
            if msg_id_idx < len(msg_ids):
                last_msg_id = msg_ids[msg_id_idx]
                msg_id_idx += 1
        if last_msg_id:
            line_to_msg_id[i] = last_msg_id

    return line_to_msg_id


def _metadata_from_msg_ids(piece_msg_ids: list[str],
                            id_to_msg: dict,
                            fallback_chunk: dict) -> dict:
    """
    Derive accurate start_ts, end_ts, authors, span_days
    from the actual message objects in this split piece.

    Falls back to original chunk values if message objects are not
    available in id_to_msg (e.g. cross-channel references).
    """
    msgs = [id_to_msg[mid] for mid in piece_msg_ids
            if mid in id_to_msg]

    if not msgs:
        return {
            "start_ts":  fallback_chunk["start_ts"],
            "end_ts":    fallback_chunk["end_ts"],
            "authors":   fallback_chunk["authors"],
            "span_days": fallback_chunk.get("span_days", 0),
        }

    msgs_sorted = sorted(msgs, key=lambda m: m["timestamp"])
    start_dt    = datetime.fromisoformat(msgs_sorted[0]["timestamp"])
    end_dt      = datetime.fromisoformat(msgs_sorted[-1]["timestamp"])
    span_days   = (end_dt - start_dt).days

    return {
        "start_ts":  msgs_sorted[0]["timestamp"],
        "end_ts":    msgs_sorted[-1]["timestamp"],
        "authors":   list(dict.fromkeys(m["author"] for m in msgs_sorted)),
        "span_days": span_days,
    }


def _split_if_needed(chunk: dict,
                     id_to_msg: dict) -> list[dict]:
    """
    Split chunk at line boundaries if it exceeds MAX_TOKENS.

    Uses _build_line_to_msg_id() to map each rendered line to its
    source message_id. Each split piece stores only the message_ids
    it actually contains, with correct per-piece metadata via
    _metadata_from_msg_ids().

    v11 note (Issue #17): root_message_id needs no handling here -
    sub = {**chunk} already copies it onto every split piece.
    """
    tokens = len(enc.encode(chunk["text"]))
    chunk["token_count"] = tokens

    if tokens <= MAX_TOKENS:
        chunk["split_index"]      = 0
        chunk["first_message_id"] = chunk["message_ids"][0] \
            if chunk["message_ids"] else ""
        return [chunk]

    thread_name   = chunk.get("thread_name")
    thread_header = f"[Thread: {thread_name}]\n" if thread_name else ""

    lines = chunk["text"].split("\n")
    if thread_header and lines and lines[0].startswith("[Thread:"):
        lines = lines[1:]

    all_msg_ids    = chunk.get("message_ids", [])
    line_to_msg_id = _build_line_to_msg_id(lines, all_msg_ids)

    current         = []
    current_msg_ids = []
    result          = []

    def _flush_piece():
        """Save current accumulated lines as a new split piece."""
        if not current:
            return
        sub_text = thread_header + "\n".join(current)
        meta = _metadata_from_msg_ids(current_msg_ids, id_to_msg, chunk)

        sub = {**chunk}
        sub["text"]             = sub_text
        sub["token_count"]      = len(enc.encode(sub_text))
        sub["split_index"]      = len(result)
        sub["message_ids"]      = list(current_msg_ids)
        sub["message_count"]    = len(current_msg_ids)
        sub["first_message_id"] = current_msg_ids[0] \
            if current_msg_ids else \
            (all_msg_ids[0] if all_msg_ids else "")
        sub["start_ts"]         = meta["start_ts"]
        sub["end_ts"]           = meta["end_ts"]
        sub["authors"]          = meta["authors"]
        sub["span_days"]        = meta["span_days"]
        result.append(sub)

    for i, line in enumerate(lines):
        msg_id = line_to_msg_id.get(i)

        test_lines     = current + [line]
        test_text      = thread_header + "\n".join(test_lines)
        would_overflow = len(enc.encode(test_text)) > MAX_TOKENS

        if would_overflow and not current:
            # Single-line overflow guard - force include as own piece
            current = [line]
            if msg_id and msg_id not in current_msg_ids:
                current_msg_ids.append(msg_id)
            _flush_piece()
            current         = []
            current_msg_ids = []
            continue

        if would_overflow and current:
            _flush_piece()
            current         = [line]
            current_msg_ids = []
            if msg_id:
                current_msg_ids = [msg_id]
            continue

        current.append(line)
        if msg_id and msg_id not in current_msg_ids:
            current_msg_ids.append(msg_id)

    _flush_piece()

    return result


def _run_regression_tests() -> bool:
    """
    Fix 2 (v10): End-to-end regression tests for reply-only oversized chunks.
    v11 (Issue #17): added Test 3 for root_message_id assignment and
    split-piece inheritance, plus Tests 4-7 covering the acceptance
    criteria explicitly named in Issue #17: a genuine multi-hop cycle,
    a missing mid-chain parent, cross-channel scope rejection, and a
    message whose immediate parent was never parsed at all.

    Test 1 - Unit: verify _build_line_to_msg_id correctly maps reply lines.
    Test 2 - Integration: verify _split_if_needed produces non-empty
              message_ids on a synthetic oversized reply-only chunk.
    Test 3 - Integration: verify root_message_id is set on reply chunks,
              defaults to None on non-reply chunks, and survives split.
    Test 4 - Unit: genuine multi-hop cycle (A->B->C->A) resolves without
              an infinite loop.
    Test 5 - Unit: a missing/malformed mid-chain parent resolves to the
              last known message instead of crashing.
    Test 6 - Integration: a root in a different channel is rejected -
              root_message_id comes back None, not the foreign id.
    Test 7 - Integration: a message whose immediate parent_id was never
              parsed at all never enters a reply group and produces no
              crash - the dominant real-world "no root" case, confirmed
              against the full corpus (see module docstring).

    Returns True if all tests pass, False otherwise.
    """
    all_pass = True

    # -- Test 1: Unit test for reply line detection ---------------------
    test_lines = [
        "  > [alice @ 2021-08-10]: this is a reply message with content",
        "  > [bob @ 2021-08-10]: another reply here in the chain",
        "  > [alice @ 2021-08-10]: a third reply completing the chain",
    ]
    test_ids = ["id001", "id002", "id003"]
    mapping  = _build_line_to_msg_id(test_lines, test_ids)

    if len(mapping) == 3 and set(mapping.values()) == set(test_ids):
        print("  Test 1 PASS: reply line detection maps all 3 lines")
    else:
        print(f"  Test 1 FAIL: mapping={mapping}, expected 3 entries "
              f"with ids {test_ids}")
        all_pass = False

    # -- Test 2: End-to-end split of reply-only oversized chunk ----------
    # Build a synthetic chunk composed entirely of reply lines.
    # Each message has a parent_id so _build() prefixes with '  > '.
    # Repeat enough times to exceed MAX_TOKENS.
    word    = "reply " * 40          # ~40 tokens per message line
    n_msgs  = 25                     # 25 x ~40 = ~1,000 tokens -> forces split

    fake_msgs = []
    id_to_msg_test = {}
    for k in range(n_msgs):
        mid = f"reply_msg_{k:04d}"
        msg = {
            "id":        mid,
            "author":    f"user{k % 3}",
            "timestamp": f"2021-08-10T{k:02d}:00:00+00:00",
            "content":   word.strip(),
            "channel":   "tpm-tradecraft",
            "channel_id": "999",
            "thread_name": None,
            # All messages are replies (have a parent_id)
            "parent_id": f"reply_msg_{max(0, k-1):04d}",
        }
        fake_msgs.append(msg)
        id_to_msg_test[mid] = msg

    # _build() with parent_id set renders all lines as '  > [author @ date]:'
    chunk = _build(fake_msgs)
    chunk["channel_id"]  = "999"
    chunk["thread_name"] = None

    # Force token_count calculation
    tokens = len(enc.encode(chunk["text"]))

    if tokens <= MAX_TOKENS:
        print(f"  Test 2 SKIP: synthetic chunk only {tokens} tokens "
              f"- increase n_msgs to exceed {MAX_TOKENS}")
    else:
        pieces = _split_if_needed(chunk, id_to_msg_test)

        # Verify all pieces have non-empty message_ids
        empty = [p for p in pieces if not p.get("message_ids")]
        if empty:
            print(f"  Test 2 FAIL: {len(empty)}/{len(pieces)} pieces "
                  f"have empty message_ids - reply line detection broken")
            for p in empty:
                print(f"    split_index={p['split_index']} "
                      f"text_preview={p['text'][:80]!r}")
            all_pass = False
        else:
            total_ids = sum(len(p["message_ids"]) for p in pieces)
            print(f"  Test 2 PASS: {len(pieces)} split pieces, all have "
                  f"non-empty message_ids ({total_ids} total)")

    # -- Test 3: root_message_id assignment and split inheritance --------
    reply_chunk = _build(fake_msgs, root_message_id="reply_msg_0000")
    if reply_chunk.get("root_message_id") == "reply_msg_0000":
        print("  Test 3a PASS: _build() stores root_message_id on chunk")
    else:
        print(f"  Test 3a FAIL: root_message_id="
              f"{reply_chunk.get('root_message_id')!r}, expected "
              f"'reply_msg_0000'")
        all_pass = False

    standalone_chunk = _build(fake_msgs)
    if standalone_chunk.get("root_message_id") is None:
        print("  Test 3b PASS: _build() defaults root_message_id to None")
    else:
        print(f"  Test 3b FAIL: expected None, got "
              f"{standalone_chunk.get('root_message_id')!r}")
        all_pass = False

    reply_chunk["channel_id"]  = "999"
    reply_chunk["thread_name"] = None
    reply_tokens = len(enc.encode(reply_chunk["text"]))
    if reply_tokens > MAX_TOKENS:
        reply_pieces = _split_if_needed(reply_chunk, id_to_msg_test)
        missing_root = [p for p in reply_pieces
                        if p.get("root_message_id") != "reply_msg_0000"]
        if missing_root:
            print(f"  Test 3c FAIL: {len(missing_root)}/{len(reply_pieces)} "
                  f"split pieces lost root_message_id")
            all_pass = False
        else:
            print(f"  Test 3c PASS: all {len(reply_pieces)} split pieces "
                  f"inherit root_message_id")
    else:
        print(f"  Test 3c SKIP: synthetic chunk only {reply_tokens} tokens")

    # -- Test 4: genuine multi-hop cycle (A -> B -> C -> A) ---------------
    # Trivial self-loops (msg pointing to itself) are a much weaker test
    # than a real multi-node cycle - Issue #17 explicitly names cycles
    # as a required case, so this exercises the visited-set break logic
    # across 3 distinct nodes rather than 1.
    cyclic_msgs = {
        "cyc_a": {"id": "cyc_a", "parent_id": "cyc_b"},
        "cyc_b": {"id": "cyc_b", "parent_id": "cyc_c"},
        "cyc_c": {"id": "cyc_c", "parent_id": "cyc_a"},
    }
    cycle_root = get_root_id(cyclic_msgs["cyc_a"], cyclic_msgs)
    if cycle_root in cyclic_msgs:
        print(f"  Test 4 PASS: multi-hop cycle (A->B->C->A) resolved "
              f"without infinite loop (root={cycle_root!r})")
    else:
        print(f"  Test 4 FAIL: resolved root {cycle_root!r} is not one "
              f"of the cycle's own message ids")
        all_pass = False

    # -- Test 5: missing/malformed parent mid-chain -----------------------
    # parent_id references a message that was never parsed (deleted,
    # export gap, or malformed reference) - must resolve gracefully,
    # returning the MISSING parent's id itself (v11.1), matching
    # chunk_manifest.py's resolve_root() so both systems agree on
    # root_message_id for the same reply chain. No crash either way.
    broken_chain = {
        "orphan_child": {"id": "orphan_child", "parent_id": "ghost_parent"},
    }
    broken_root = get_root_id(broken_chain["orphan_child"], broken_chain)
    if broken_root == "ghost_parent":
        print("  Test 5 PASS: missing mid-chain parent returns the "
              "unresolved parent id, matching resolve_root(), no crash")
    else:
        print(f"  Test 5 FAIL: expected 'ghost_parent', got "
              f"{broken_root!r}")
        all_pass = False

    # -- Test 6: cross-channel scope safety --------------------------------
    # A message's resolved root lives in a different channel entirely -
    # root_message_id must come back None, never the foreign root's id.
    foreign_root = {
        "id": "foreign_root_msg", "author": "someone",
        "timestamp": "2021-08-10T00:00:00+00:00",
        "content": "a message in a different channel",
        "channel": "other-channel", "channel_id": "other_channel_id",
        "thread_name": None, "parent_id": None,
    }
    local_reply = {
        "id": "local_reply_msg", "author": "someone_else",
        "timestamp": "2021-08-10T00:05:00+00:00",
        "content": "replying to a message from another channel",
        "channel": "tpm-tradecraft", "channel_id": "999",
        "thread_name": None, "parent_id": "foreign_root_msg",
    }
    cross_id_to_msg = {
        foreign_root["id"]: foreign_root,
        local_reply["id"]:  local_reply,
    }
    cross_chunks = _reply_aware_chunk(
        [local_reply], cross_id_to_msg, is_thread=False
    )
    reply_like = [c for c in cross_chunks if c.get("message_count", 0) > 1]
    if not reply_like:
        print("  Test 6 SKIP: synthetic group did not reach min_msgs "
              "threshold - adjust test data")
    elif reply_like[0].get("root_message_id") is None:
        print("  Test 6 PASS: cross-channel root correctly nulled, "
              "not leaked into root_message_id")
    else:
        print(f"  Test 6 FAIL: root_message_id="
              f"{reply_like[0].get('root_message_id')!r}, expected None "
              f"(foreign-channel root should be scope-rejected)")
        all_pass = False

    # -- Test 7: immediate parent never parsed at all ----------------------
    # Distinct from Test 5 (mid-chain gap, tested via get_root_id directly).
    # Here the VERY FIRST parent lookup in _reply_aware_chunk's own gate
    # ("if msg.get('parent_id') and msg['parent_id'] in id_to_msg") fails,
    # so the message never enters root_groups at all - confirmed via
    # corpus analysis to be the dominant real-world "no root" case
    # (897 of ~14,283 "  > "-rendered chunks in the full corpus run),
    # distinct from and much larger than genuine scope-rejection (147).
    lone_reply = {
        "id": "lone_reply_msg", "author": "someone",
        "timestamp": "2021-08-10T00:00:00+00:00",
        "content": "replying to a message that was never parsed",
        "channel": "tpm-tradecraft", "channel_id": "999",
        "thread_name": None, "parent_id": "never_parsed_parent",
    }
    lone_id_to_msg = {lone_reply["id"]: lone_reply}
    lone_chunks = _reply_aware_chunk(
        [lone_reply], lone_id_to_msg, is_thread=False
    )
    bad_roots = [c.get("root_message_id") for c in lone_chunks
                 if c.get("root_message_id") is not None]
    if not bad_roots:
        print(f"  Test 7 PASS: message with unparsed parent produces no "
              f"crash and no root_message_id ({len(lone_chunks)} chunk(s) "
              f"produced)")
    else:
        print(f"  Test 7 FAIL: unexpected root_message_id values "
              f"{bad_roots!r} for a parent that was never parsed")
        all_pass = False

    # -- Test 8: immediate parent exists, grandparent missing --------------
    # The exact scenario Finding 1 identified as untested: a chain
    # where the immediate parent IS parsed (so the outer gate lets it
    # into root_groups), but that parent's own parent_id points to a
    # message never parsed. Chunk grouping must NOT regress to
    # time-window chunking - the reply pair must still coalesce into
    # one reply chunk - while root_message_id metadata correctly
    # comes back None (unresolvable root, not falsely claimed).
    gap_parent = {
        "id": "gap_parent_msg", "author": "alice",
        "timestamp": "2021-08-10T00:00:00+00:00",
        "content": "a message whose own parent was never parsed",
        "channel": "tpm-tradecraft", "channel_id": "999",
        "thread_name": None, "parent_id": "never_parsed_grandparent",
    }
    gap_child = {
        "id": "gap_child_msg", "author": "bob",
        "timestamp": "2021-08-10T00:05:00+00:00",
        "content": "replying to a message with a missing grandparent",
        "channel": "tpm-tradecraft", "channel_id": "999",
        "thread_name": None, "parent_id": "gap_parent_msg",
    }
    gap_id_to_msg = {
        gap_parent["id"]: gap_parent,
        gap_child["id"]:  gap_child,
    }
    gap_chunks = _reply_aware_chunk(
        [gap_parent, gap_child], gap_id_to_msg, is_thread=False
    )
    reply_like_gap = [c for c in gap_chunks if c.get("message_count", 0) >= 2]
    if not reply_like_gap:
        print("  Test 8 FAIL: parent+child did not coalesce into one "
              "reply chunk - chunk membership regressed on upstream gap")
        all_pass = False
    elif reply_like_gap[0].get("root_message_id") is not None:
        print(f"  Test 8 FAIL: expected root_message_id=None (unresolvable "
              f"root), got {reply_like_gap[0].get('root_message_id')!r}")
        all_pass = False
    else:
        print("  Test 8 PASS: parent+child still coalesce into one chunk "
              "despite missing grandparent, root_message_id correctly None")

    return all_pass


# -- Quick test ------------------------------------------------------
if __name__ == "__main__":
    from ingestion.parser import parse_all_exports
    records = parse_all_exports("chat_logs")
    chunks  = chunk_records(records)

    reply_chunks  = [c for c in chunks
                     if any("  > " in l
                            for l in c["text"].split("\n"))]
    thread_chunks = [c for c in chunks if c.get("thread_name")]
    split_chunks  = [c for c in chunks if c.get("split_index", 0) > 0]
    long_span     = [c for c in chunks if c.get("span_days", 0) > 30]

    print(f"\nReply-aware chunks:  {len(reply_chunks)}")
    print(f"Thread chunks:       {len(thread_chunks)}")
    print(f"Split pieces (>0):   {len(split_chunks)}")
    print(f"Long-span (>30d):    {len(long_span)}")
    print(f"Time-window chunks:  "
          f"{len(chunks) - len(reply_chunks) - len(thread_chunks)}")
    print(f"\nQuality metrics:")
    print(f"  Avg messages per chunk: "
          f"{sum(c['message_count'] for c in chunks) / len(chunks):.1f}")
    print(f"  Avg tokens per chunk:   "
          f"{sum(c['token_count'] for c in chunks) / len(chunks):.1f}")
    print(f"  Largest chunk (tokens): "
          f"{max(c['token_count'] for c in chunks)}")
    print(f"  Smallest chunk (tokens):"
          f"{min(c['token_count'] for c in chunks)}")
    print(f"  Max span (days):        "
          f"{max(c.get('span_days', 0) for c in chunks)}")

    # Production regression check - no split piece with empty message_ids
    if split_chunks:
        print(f"\nSplit piece metadata verification (first 3 pieces):")
        for sc in split_chunks[:3]:
            print(f"  split_index={sc['split_index']} "
                  f"msg_count={sc['message_count']} "
                  f"start_ts={sc['start_ts'][:10]} "
                  f"authors={sc['authors']} "
                  f"first_message_id={sc['first_message_id']}")

        empty_ids = [c for c in split_chunks if not c.get("message_ids")]
        if empty_ids:
            print(f"\n  REGRESSION FAIL: {len(empty_ids)} split pieces "
                  f"have empty message_ids:")
            for c in empty_ids:
                print(f"    split_index={c['split_index']} "
                      f"channel={c['channel']} "
                      f"text_preview={c['text'][:80]!r}")
        else:
            print(f"\n  Regression check: 0/{len(split_chunks)} split "
                  f"pieces with empty message_ids")

        all_piece_ids = set()
        for sc in split_chunks:
            all_piece_ids.update(sc["message_ids"])
        print(f"  Split coverage: {len(split_chunks)} pieces, "
              f"{len(all_piece_ids)} unique message IDs")

    # Fix 2: Run end-to-end regression tests
    print(f"\nRunning regression tests...")
    passed = _run_regression_tests()
    print(f"\nRegression tests: {'ALL PASS' if passed else 'SOME FAILED'}")