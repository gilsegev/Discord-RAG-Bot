"""
ingestion/chunker.py — v10 reply-aware chunking
v10 fixes (post PR #5):
  - Fix 1: reply line detection in _build_line_to_msg_id() now handles
            '  > [author @ date]:' format — previously lstrip() left '> ['
            which failed the is_msg_line check, producing empty message_ids
            on reply-only split chunks (2/11,442 in tpm-tradecraft)
  - Fix 2: end-to-end regression test for oversized chunk composed
            entirely of reply-rendered lines — verifies _split_if_needed
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
    Cross-channel reply references handled gracefully.
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
    """
    min_msgs = MIN_MSGS_THREAD if is_thread else MIN_MSGS

    root_groups = {}
    assigned    = set()

    for msg in msgs:
        if msg.get("parent_id") and msg["parent_id"] in id_to_msg:
            root_id = get_root_id(msg, id_to_msg)
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
            reply_chunks.append(_build(group_msgs))
        else:
            orphans.extend(group_msgs)

    # Add orphans BEFORE calling _window_chunk (critical ordering)
    standalone.extend(orphans)
    time_chunks = _window_chunk(standalone, min_msgs=min_msgs)

    return reply_chunks + time_chunks


def _window_chunk(msgs: list[dict],
                  min_msgs: int = MIN_MSGS) -> list[dict]:
    """
    15-min time window chunking — fallback for non-reply messages.
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
            chunks.append(_build(current))
            current  = prev_tail + [msg]
            start_ts = datetime.fromisoformat(msg["timestamp"])
        else:
            current.append(msg)

    if current and len(current) >= min_msgs:
        chunks.append(_build(current))

    return chunks


def _build(msgs: list[dict]) -> dict:
    """
    Format a list of messages into one chunk dict.
    thread_name prepended to chunk text for semantic retrieval.
    span_days calculated for long-span metadata filtering.
    dict.fromkeys preserves insertion order while deduplicating authors.
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
        "text":          "\n".join(lines),
        "start_ts":      msgs[0]["timestamp"],
        "end_ts":        msgs[-1]["timestamp"],
        "channel":       msgs[0]["channel"],
        "channel_id":    msgs[0].get("channel_id"),
        "thread_name":   thread_name,
        "authors":       list(dict.fromkeys(m["author"] for m in msgs)),
        "message_count": len(msgs),
        "message_ids":   [m["id"] for m in msgs],
        "span_days":     span_days,
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
            # Single-line overflow guard — force include as own piece
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
    Fix 2: End-to-end regression tests for reply-only oversized chunks.

    Test 1 — Unit: verify _build_line_to_msg_id correctly maps reply lines.
    Test 2 — Integration: verify _split_if_needed produces non-empty
              message_ids on a synthetic oversized reply-only chunk.

    Returns True if all tests pass, False otherwise.
    """
    all_pass = True

    # ── Test 1: Unit test for reply line detection ──────────────────
    test_lines = [
        "  > [alice @ 2021-08-10]: this is a reply message with content",
        "  > [bob @ 2021-08-10]: another reply here in the chain",
        "  > [alice @ 2021-08-10]: a third reply completing the chain",
    ]
    test_ids = ["id001", "id002", "id003"]
    mapping  = _build_line_to_msg_id(test_lines, test_ids)

    if len(mapping) == 3 and set(mapping.values()) == set(test_ids):
        print("  Test 1 PASS: reply line detection maps all 3 lines ✓")
    else:
        print(f"  Test 1 FAIL: mapping={mapping}, expected 3 entries "
              f"with ids {test_ids}")
        all_pass = False

    # ── Test 2: End-to-end split of reply-only oversized chunk ──────
    # Build a synthetic chunk composed entirely of reply lines.
    # Each message has a parent_id so _build() prefixes with '  > '.
    # Repeat enough times to exceed MAX_TOKENS.
    word    = "reply " * 40          # ~40 tokens per message line
    n_msgs  = 25                     # 25 × ~40 = ~1,000 tokens → forces split

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
              f"— increase n_msgs to exceed {MAX_TOKENS}")
    else:
        pieces = _split_if_needed(chunk, id_to_msg_test)

        # Verify all pieces have non-empty message_ids
        empty = [p for p in pieces if not p.get("message_ids")]
        if empty:
            print(f"  Test 2 FAIL: {len(empty)}/{len(pieces)} pieces "
                  f"have empty message_ids — reply line detection broken")
            for p in empty:
                print(f"    split_index={p['split_index']} "
                      f"text_preview={p['text'][:80]!r}")
            all_pass = False
        else:
            total_ids = sum(len(p["message_ids"]) for p in pieces)
            print(f"  Test 2 PASS: {len(pieces)} split pieces, all have "
                  f"non-empty message_ids ({total_ids} total) ✓")

    return all_pass


# ── Quick test ────────────────────────────────────────────────
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

    # Production regression check — no split piece with empty message_ids
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
                  f"pieces with empty message_ids ✓")

        all_piece_ids = set()
        for sc in split_chunks:
            all_piece_ids.update(sc["message_ids"])
        print(f"  Split coverage: {len(split_chunks)} pieces, "
              f"{len(all_piece_ids)} unique message IDs")

    # Fix 2: Run end-to-end regression tests
    print(f"\nRunning regression tests...")
    passed = _run_regression_tests()
    print(f"\nRegression tests: {'ALL PASS ✓' if passed else 'SOME FAILED ✗'}")