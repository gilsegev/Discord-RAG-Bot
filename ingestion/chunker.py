"""
ingestion/chunker.py — v4 reply-aware chunking
Fixes applied:
  - Fix #4: filtered root message handling (bot/system roots)
  - Fix #6: start_ts reset uses current msg not overlap tail
  - Fix #12: author order preserved with dict.fromkeys()
Author: ThinkInSystems (Hemanth Aragonda)
"""
import tiktoken
from datetime import datetime

enc          = tiktoken.get_encoding("cl100k_base")
WINDOW_MINS  = 15
MIN_MSGS     = 2
MAX_TOKENS   = 700
# Used only in _window_chunk fallback — not in reply-aware path
OVERLAP_MSGS = 2


def chunk_records(records: list[dict]) -> list[dict]:
    """
    Main entry point. Returns chunks ready for embedding.
    Pass 1: reply-aware grouping via parent_id chains.
    Pass 2: 15-min time window fallback for standalone messages.
    """
    id_to_msg = {r["id"]: r for r in records}

    by_channel = {}
    for r in records:
        by_channel.setdefault(r["channel"], []).append(r)

    all_chunks = []
    for channel, msgs in by_channel.items():
        msgs = sorted(msgs, key=lambda m: m["timestamp"])
        chunks = _reply_aware_chunk(msgs, id_to_msg)
        for chunk in chunks:
            all_chunks.extend(_split_if_needed(chunk))

    all_chunks.sort(key=lambda c: c["start_ts"])

    print(f"Created {len(all_chunks)} chunks from "
          f"{len(records)} messages across "
          f"{len(by_channel)} channel(s)")
    return all_chunks


def get_root_id(msg: dict, id_to_msg: dict) -> str:
    """
    Follow parent_id chain to find the root message id.
    Cycle detection via visited set prevents infinite loops.
    Cross-channel reply references handled gracefully —
    if parent is in a different channel it won't be in id_to_msg
    and the loop exits, treating the message as a local root.
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


def _reply_aware_chunk(msgs: list[dict], id_to_msg: dict) -> list[dict]:
    """
    Two-pass chunking:
    Pass 1: group reply chains by parent_id.
    Pass 2: time window for standalone + orphaned messages.

    Fix #4: if root message was filtered (bot/system), replies
    are treated as standalone rather than context-free chunks.
    Orphans collected BEFORE _window_chunk call (critical ordering).
    """
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
        # Fix #4: if root was filtered (bot/system message),
        # it won't be in id_to_msg. Treat replies as standalone
        # rather than producing context-free answer-only chunks.
        if root_id not in id_to_msg and \
                not any(m["id"] == root_id for m in group_msgs):
            orphans.extend(group_msgs)
            continue

        group_msgs = sorted(group_msgs, key=lambda m: m["timestamp"])
        if len(group_msgs) >= MIN_MSGS:
            reply_chunks.append(_build(group_msgs))
        else:
            # Single-message groups fall back to time-window
            orphans.extend(group_msgs)

    # Add orphans BEFORE calling _window_chunk
    standalone.extend(orphans)
    time_chunks = _window_chunk(standalone)

    return reply_chunks + time_chunks


def _window_chunk(msgs: list[dict]) -> list[dict]:
    """
    15-min time window chunking — fallback for non-reply messages.
    Fix #6: start_ts resets to current message timestamp after split,
    not to overlap tail timestamp. Prevents window extension by overlap.
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

        if gap > WINDOW_MINS and len(current) >= MIN_MSGS:
            prev_tail = current[-OVERLAP_MSGS:]
            chunks.append(_build(current))
            current  = prev_tail + [msg]
            # Fix #6: use current msg timestamp not overlap tail
            # prevents overlap messages extending the window anchor
            start_ts = datetime.fromisoformat(msg["timestamp"])
        else:
            current.append(msg)

    if current and len(current) >= MIN_MSGS:
        chunks.append(_build(current))

    return chunks


def _build(msgs: list[dict]) -> dict:
    """
    Format a list of messages into one chunk dict.
    Fix #12: authors use dict.fromkeys() to preserve insertion
    order while deduplicating — set comprehension is unordered.
    """
    assert msgs, "_build() called with empty message list"

    lines = []
    for m in msgs:
        date = m["timestamp"][:10]
        line = f"[{m['author']} @ {date}]: {m['content']}"
        if m.get("parent_id"):
            line = "  > " + line
        lines.append(line)

    return {
        "text":          "\n".join(lines),
        "start_ts":      msgs[0]["timestamp"],
        "end_ts":        msgs[-1]["timestamp"],
        "channel":       msgs[0]["channel"],
        # dict.fromkeys preserves insertion order while deduplicating
        "authors":       list(dict.fromkeys(m["author"] for m in msgs)),
        "message_count": len(msgs),
        "message_ids":   [m["id"] for m in msgs],
    }


def _split_if_needed(chunk: dict) -> list[dict]:
    """Split chunk at line boundaries if it exceeds MAX_TOKENS."""
    tokens = len(enc.encode(chunk["text"]))
    chunk["token_count"] = tokens

    if tokens <= MAX_TOKENS:
        return [chunk]

    lines, current, result = chunk["text"].split("\n"), [], []
    for line in lines:
        current.append(line)
        if len(enc.encode("\n".join(current))) > MAX_TOKENS:
            current.pop()
            if current:
                sub = {**chunk, "text": "\n".join(current)}
                sub["token_count"] = len(enc.encode(sub["text"]))
                result.append(sub)
            current = [line]

    if current:
        sub = {**chunk, "text": "\n".join(current)}
        sub["token_count"] = len(enc.encode(sub["text"]))
        result.append(sub)

    return result


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    from parser import parse_all_exports
    records = parse_all_exports("chat_logs")
    chunks  = chunk_records(records)

    reply_chunks = [c for c in chunks
                    if any("  > " in l for l in c["text"].split("\n"))]
    print(f"\nReply-aware chunks: {len(reply_chunks)}")
    print(f"Time-window chunks: {len(chunks) - len(reply_chunks)}")
    print(f"\nQuality metrics:")
    print(f"  Resolved reply pairs in chunks: {len(reply_chunks)}")
    print(f"  Avg messages per chunk: "
          f"{sum(c['message_count'] for c in chunks) / len(chunks):.1f}")
    print(f"  Avg tokens per chunk:   "
          f"{sum(c['token_count'] for c in chunks) / len(chunks):.1f}")
    print(f"  Largest chunk (tokens): "
          f"{max(c['token_count'] for c in chunks)}")
    print(f"  Smallest chunk (tokens): "
          f"{min(c['token_count'] for c in chunks)}")