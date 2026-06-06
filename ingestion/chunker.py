"""
ingestion/chunker.py — v8 reply-aware chunking
v8 fixes (PR #4):
  - Fix 1: split pieces now track actual message_ids per piece
            first_message_id = message_ids[0] for each piece
            Full original message_ids no longer shared across pieces
            Fixes wrong Discord links and weak dedupe on split chunks
  - Fix 2: smoke test import updated to ingestion.parser
  - Fix 3: single-line overflow guard — prevents empty piece on long lines
  - Fix 4: message_count updated per split piece
  - Fix 5: dead chunk param removed from _build_line_to_msg_id
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
            all_chunks.extend(_split_if_needed(chunk))

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
    Continuation lines (rare, from multi-line messages) inherit
    the same message_id as the preceding message line.

    Fix 5: removed unused chunk parameter from signature.
    """
    line_to_msg_id = {}
    msg_id_idx     = 0
    last_msg_id    = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # Message line: starts with '[' after optional reply indent
        is_msg_line = (
            stripped.startswith("[") and
            "@ " in stripped and
            "]:" in stripped
        )
        if is_msg_line:
            if msg_id_idx < len(msg_ids):
                last_msg_id = msg_ids[msg_id_idx]
                msg_id_idx += 1
        # Assign current message_id to this line
        if last_msg_id:
            line_to_msg_id[i] = last_msg_id

    return line_to_msg_id


def _split_if_needed(chunk: dict) -> list[dict]:
    """
    Split chunk at line boundaries if it exceeds MAX_TOKENS.

    Fix 1 (PR #4 — confirmed bug by gilsegev):
    Each split piece now tracks its own actual message_ids and
    first_message_id based on which message lines are actually
    rendered in that piece — not positional index into the original
    message_ids array.

    Fix 3: Single-line overflow guard — if one line alone exceeds
    MAX_TOKENS, force-include it as its own piece rather than
    producing an empty piece or looping indefinitely.

    Fix 4: message_count updated per split piece to reflect
    actual messages in that piece, not the original full chunk count.
    """
    tokens = len(enc.encode(chunk["text"]))
    chunk["token_count"] = tokens

    if tokens <= MAX_TOKENS:
        chunk["split_index"]      = 0
        chunk["first_message_id"] = chunk["message_ids"][0] \
            if chunk["message_ids"] else ""
        return [chunk]

    # Extract thread title — prepend to every split piece
    thread_name   = chunk.get("thread_name")
    thread_header = f"[Thread: {thread_name}]\n" if thread_name else ""

    lines = chunk["text"].split("\n")
    # Skip existing thread header line — re-added to each piece
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
        sub = {**chunk}
        sub["text"]             = sub_text
        sub["token_count"]      = len(enc.encode(sub_text))
        sub["split_index"]      = len(result)
        # Fix 1: actual message_ids in this piece only
        sub["message_ids"]      = list(current_msg_ids)
        # Fix 4: message_count reflects actual messages in this piece
        sub["message_count"]    = len(current_msg_ids)
        sub["first_message_id"] = current_msg_ids[0] \
            if current_msg_ids else \
            (all_msg_ids[0] if all_msg_ids else "")
        result.append(sub)

    for i, line in enumerate(lines):
        msg_id = line_to_msg_id.get(i)

        # Fix 3: single-line overflow guard
        # If adding this line would overflow and current is already empty,
        # the line itself is oversized — force-include it alone.
        test_lines = current + [line]
        test_text  = thread_header + "\n".join(test_lines)
        would_overflow = len(enc.encode(test_text)) > MAX_TOKENS

        if would_overflow and not current:
            # Force-include oversized single line as its own piece
            current = [line]
            if msg_id and msg_id not in current_msg_ids:
                current_msg_ids.append(msg_id)
            _flush_piece()
            current         = []
            current_msg_ids = []
            continue

        if would_overflow and current:
            # Normal overflow — flush current piece, start new with this line
            _flush_piece()
            current         = [line]
            current_msg_ids = []
            if msg_id:
                current_msg_ids = [msg_id]
            continue

        # No overflow — accumulate
        current.append(line)
        if msg_id and msg_id not in current_msg_ids:
            current_msg_ids.append(msg_id)

    # Flush final piece
    _flush_piece()

    return result


# ── Quick test (Fix 2: use ingestion.parser not parser) ───────
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

    # Verify split piece message_ids are correct
    if split_chunks:
        print(f"\nSplit piece verification (first 3 pieces):")
        for sc in split_chunks[:3]:
            print(f"  split_index={sc['split_index']} "
                  f"msg_count={sc['message_count']} "
                  f"first_message_id={sc['first_message_id']} "
                  f"message_ids={sc['message_ids'][:3]}"
                  f"{'...' if len(sc['message_ids']) > 3 else ''}")

        # Coverage check — no message IDs lost across pieces
        from itertools import groupby
        sorted_splits = sorted(split_chunks,
                               key=lambda c: str(c.get("message_ids", [])))
        print(f"\nSplit coverage check:")
        print(f"  Total split pieces: {len(split_chunks)}")
        all_piece_ids = set()
        for sc in split_chunks:
            all_piece_ids.update(sc["message_ids"])
        print(f"  Unique message IDs across all pieces: {len(all_piece_ids)}")