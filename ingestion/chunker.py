"""
ingestion/chunker.py — v9 reply-aware chunking
v9 fixes (PR #5):
  - Fix 1: split pieces now update start_ts, end_ts, authors, span_days
            per piece — previously inherited stale values from original chunk
            gilsegev validation: 11/14 stale start_ts, 13/14 stale authors
  - id_to_msg passed into _split_if_needed() to look up actual message objects
Prior fixes retained from v8:
  - Per-piece message_ids and first_message_id (PR #4)
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
            # Fix 1: pass id_to_msg so split pieces can look up message objects
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

    Strategy: walk lines in order. When a line starts with
    '[author @ date]:' or '  > [author @ date]:' it's a new message.
    Assign it the next message_id from msg_ids in order.
    Continuation lines inherit the same message_id as the preceding
    message line.
    """
    line_to_msg_id = {}
    msg_id_idx     = 0
    last_msg_id    = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
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
    Fix 1: derive accurate start_ts, end_ts, authors, span_days
    from the actual message objects in this split piece.

    Falls back to original chunk values if message objects are not
    available in id_to_msg (e.g. cross-channel references).
    """
    msgs = [id_to_msg[mid] for mid in piece_msg_ids
            if mid in id_to_msg]

    if not msgs:
        # Fallback — should not happen in practice
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

    Fix 1 (v9): start_ts, end_ts, authors, span_days now derived from
    actual message objects in each split piece via _metadata_from_msg_ids().
    Previously all split pieces inherited stale values from {**chunk}.
    gilsegev validation: 11/14 stale start_ts, 13/14 stale authors fixed.

    Fix from v8 retained:
    - Per-piece message_ids and first_message_id
    - Single-line overflow guard
    - message_count per piece
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

        # Fix 1: derive accurate metadata from actual messages in this piece
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
        # Fix 1: overwrite stale timestamps and authors from {**chunk}
        sub["start_ts"]         = meta["start_ts"]
        sub["end_ts"]           = meta["end_ts"]
        sub["authors"]          = meta["authors"]
        sub["span_days"]        = meta["span_days"]
        result.append(sub)

    for i, line in enumerate(lines):
        msg_id = line_to_msg_id.get(i)

        test_lines    = current + [line]
        test_text     = thread_header + "\n".join(test_lines)
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

    # Verify split piece metadata is correct
    if split_chunks:
        print(f"\nSplit piece metadata verification (first 3 pieces):")
        for sc in split_chunks[:3]:
            print(f"  split_index={sc['split_index']} "
                  f"msg_count={sc['message_count']} "
                  f"start_ts={sc['start_ts'][:10]} "
                  f"end_ts={sc['end_ts'][:10]} "
                  f"authors={sc['authors']} "
                  f"span_days={sc['span_days']} "
                  f"first_message_id={sc['first_message_id']}")

        # Coverage check
        all_piece_ids = set()
        for sc in split_chunks:
            all_piece_ids.update(sc["message_ids"])
        print(f"\nSplit coverage: {len(split_chunks)} pieces, "
              f"{len(all_piece_ids)} unique message IDs")