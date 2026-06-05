"""
ingestion/chunker.py — v7 reply-aware chunking
v7 fixes:
  - Fix 1: split_index added to each split piece for unique Qdrant IDs
  - Fix 2: first_message_id stored per split piece (not from original chunk)
           ensures Discord links point to correct message in each piece
  - span_days metadata for long-span reply chain filtering
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
    prev_tail = []

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


def _split_if_needed(chunk: dict) -> list[dict]:
    """
    Split chunk at line boundaries if it exceeds MAX_TOKENS.
    Fix 1: each split piece gets a unique split_index so stable IDs
    don't collide in Qdrant when multiple pieces share same message_ids.
    Fix 2: first_message_id stored per split piece so Discord links
    point to the correct message in each piece, not the original chunk.
    Thread title preserved in every split piece.
    """
    tokens = len(enc.encode(chunk["text"]))
    chunk["token_count"] = tokens

    if tokens <= MAX_TOKENS:
        chunk["split_index"]     = 0
        chunk["first_message_id"] = chunk["message_ids"][0] \
            if chunk["message_ids"] else ""
        return [chunk]

    # Extract thread title — prepend to every split piece
    thread_name   = chunk.get("thread_name")
    thread_header = f"[Thread: {thread_name}]\n" if thread_name else ""

    lines = chunk["text"].split("\n")
    # Skip existing thread header — re-added to each piece
    if thread_header and lines and lines[0].startswith("[Thread:"):
        lines = lines[1:]

    current       = []
    result        = []
    current_msgs  = []  # track which messages are in current piece

    # Build message lookup for tracking first_message_id per piece
    # Each line maps to a message by position in original chunk
    msg_ids = chunk.get("message_ids", [])

    for line in lines:
        current.append(line)
        full_text = thread_header + "\n".join(current)
        if len(enc.encode(full_text)) > MAX_TOKENS:
            current.pop()
            if current:
                sub_text = thread_header + "\n".join(current)
                sub = {**chunk, "text": sub_text}
                sub["token_count"]     = len(enc.encode(sub_text))
                sub["split_index"]     = len(result)
                # first_message_id is first msg_id in this piece
                piece_idx = len(result)
                sub["first_message_id"] = msg_ids[piece_idx] \
                    if piece_idx < len(msg_ids) else \
                    (msg_ids[0] if msg_ids else "")
                result.append(sub)
            current = [line]

    if current:
        sub_text = thread_header + "\n".join(current)
        sub = {**chunk, "text": sub_text}
        sub["token_count"]     = len(enc.encode(sub_text))
        sub["split_index"]     = len(result)
        piece_idx = len(result)
        sub["first_message_id"] = msg_ids[piece_idx] \
            if piece_idx < len(msg_ids) else \
            (msg_ids[0] if msg_ids else "")
        result.append(sub)

    return result


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    from parser import parse_all_exports
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