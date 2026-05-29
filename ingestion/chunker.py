"""
ingestion/chunker.py — v2 reply-aware chunking
Addresses PR #1 feedback from gilsegev:
  - parent_id used as first-class conversation signal
  - reply pairs kept with their parent messages
  - 15-min window chunking retained as fallback
  - orphan handling fixed: orphans collected BEFORE _window_chunk call
Author: ThinkInSystems (Hemanth Aragonda)
"""
import tiktoken
from datetime import datetime

enc          = tiktoken.get_encoding("cl100k_base")
WINDOW_MINS  = 15
MIN_MSGS     = 2
MAX_TOKENS   = 700
# OVERLAP_MSGS used only in _window_chunk fallback path
OVERLAP_MSGS = 2


def chunk_records(records: list[dict]) -> list[dict]:
    """
    Main entry point. Returns chunks ready for embedding.
    Uses parent_id as first-class signal before falling back
    to 15-minute time window chunking.
    """
    # Build id -> message lookup for reply resolution
    id_to_msg = {r["id"]: r for r in records}

    # Group by channel — never mix channels in one chunk
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
    Module-level so it can be tested independently.
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
    Two-pass chunking strategy:

    Pass 1 — Reply grouping:
      Build conversation groups by following parent_id chains.
      Each group = root message + all its direct/indirect replies.
      Ensures Q&A pairs stay together regardless of time gap.

    Pass 2 — Time window chunking:
      For messages not part of a reply chain, use 15-minute
      windows as fallback. Orphaned single-message reply groups
      are added to standalone BEFORE _window_chunk is called.
    """
    # Assign every reply message to a root group
    root_groups = {}
    assigned    = set()

    for msg in msgs:
        if msg.get("parent_id") and msg["parent_id"] in id_to_msg:
            root_id = get_root_id(msg, id_to_msg)
            root_groups.setdefault(root_id, []).append(msg)
            assigned.add(msg["id"])
            # Make sure the root itself is in the group
            if root_id in id_to_msg and root_id not in assigned:
                root_groups[root_id].insert(
                    0, id_to_msg[root_id])
                assigned.add(root_id)

    # Start with messages not part of any reply chain
    standalone   = [m for m in msgs if m["id"] not in assigned]
    reply_chunks = []
    orphans      = []

    # Process reply groups first — collect orphans separately
    for root_id, group_msgs in root_groups.items():
        group_msgs = sorted(
            group_msgs, key=lambda m: m["timestamp"])
        if len(group_msgs) >= MIN_MSGS:
            reply_chunks.append(_build(group_msgs))
        else:
            # Single-message groups — collect as orphans
            # so time-window chunker can handle them
            orphans.extend(group_msgs)

    # Add orphans BEFORE calling _window_chunk
    # (previous bug: orphans were added after _window_chunk ran)
    standalone.extend(orphans)
    time_chunks = _window_chunk(standalone)

    return reply_chunks + time_chunks


def _window_chunk(msgs: list[dict]) -> list[dict]:
    """
    15-minute time window chunking.
    Fallback for non-reply messages and orphaned reply groups.
    Uses OVERLAP_MSGS overlap at chunk boundaries.
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
            start_ts = datetime.fromisoformat(
                prev_tail[0]["timestamp"])
        else:
            current.append(msg)

    if current and len(current) >= MIN_MSGS:
        chunks.append(_build(current))

    return chunks


def _build(msgs: list[dict]) -> dict:
    """
    Format a list of messages into one chunk dict.
    Asserts non-empty to catch programming errors early.
    """
    assert msgs, "_build() called with empty message list"

    lines = []
    for m in msgs:
        date = m["timestamp"][:10]
        line = f"[{m['author']} @ {date}]: {m['content']}"
        if m.get("parent_id"):
            line = "  > " + line  # indent replies visually
        lines.append(line)

    return {
        "text":          "\n".join(lines),
        "start_ts":      msgs[0]["timestamp"],
        "end_ts":        msgs[-1]["timestamp"],
        "channel":       msgs[0]["channel"],
        "authors":       list({m["author"] for m in msgs}),
        "message_count": len(msgs),
        "message_ids":   [m["id"] for m in msgs],
    }


def _split_if_needed(chunk: dict) -> list[dict]:
    """
    Split chunk at line boundaries if it exceeds MAX_TOKENS.
    Never splits mid-message.
    """
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
                    if any("  > " in l
                           for l in c["text"].split("\n"))]
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