"""
ingestion/chunker.py — v6 reply-aware chunking
v6 additions:
  - thread_name included in chunk text for semantic retrieval
  - singleton forum thread messages indexed (MIN_MSGS_THREAD=1)
  - thread_name stored in chunk payload for Qdrant filtering
  - Fix 3: thread title preserved in every split chunk piece
Author: ThinkInSystems (Hemanth Aragonda)
"""
import tiktoken
from datetime import datetime

enc             = tiktoken.get_encoding("cl100k_base")
WINDOW_MINS     = 15
MIN_MSGS        = 2    # minimum for regular channel chunks
MIN_MSGS_THREAD = 1    # forum thread singletons are meaningful
MAX_TOKENS      = 700
# Used only in _window_chunk fallback — not in reply-aware path
OVERLAP_MSGS    = 2


def chunk_records(records: list[dict]) -> list[dict]:
    """
    Main entry point. Returns chunks ready for embedding.
    Pass 1: reply-aware grouping via parent_id chains.
    Pass 2: 15-min time window fallback for standalone messages.
    Forum thread singletons indexed with MIN_MSGS_THREAD=1.
    """
    id_to_msg = {r["id"]: r for r in records}

    by_channel = {}
    for r in records:
        # Group by channel+thread to keep threads separate
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


def _reply_aware_chunk(msgs: list[dict], id_to_msg: dict,
                       is_thread: bool = False) -> list[dict]:
    """
    Two-pass chunking:
    Pass 1: group reply chains by parent_id.
    Pass 2: time window for standalone + orphaned messages.

    is_thread=True uses MIN_MSGS_THREAD to index singletons.
    Filtered root handling: if root was bot/system message,
    replies treated as standalone not context-free chunks.
    Orphans collected BEFORE _window_chunk call (critical ordering).
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
        # If root was filtered (bot/system), treat replies as standalone
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
    min_msgs parameter allows lower threshold for forum threads.
    start_ts resets to current message timestamp after split —
    not overlap tail, which would extend the window incorrectly.
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
            # Reset to current message — not overlap tail
            start_ts = datetime.fromisoformat(msg["timestamp"])
        else:
            current.append(msg)

    if current and len(current) >= min_msgs:
        chunks.append(_build(current))

    return chunks


def _build(msgs: list[dict]) -> dict:
    """
    Format a list of messages into one chunk dict.
    thread_name prepended to chunk text for semantic retrieval —
    exposes topic words directly to the embedding model.
    dict.fromkeys preserves insertion order while deduplicating authors.
    """
    assert msgs, "_build() called with empty message list"

    thread_name = msgs[0].get("thread_name")
    lines       = []

    # Prepend thread title — improves semantic retrieval
    if thread_name:
        lines.append(f"[Thread: {thread_name}]")

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
        "thread_name":   thread_name,
        "authors":       list(dict.fromkeys(m["author"] for m in msgs)),
        "message_count": len(msgs),
        "message_ids":   [m["id"] for m in msgs],
    }


def _split_if_needed(chunk: dict) -> list[dict]:
    """
    Split chunk at line boundaries if it exceeds MAX_TOKENS.
    Fix 3: thread title prepended to every split piece so
    all sub-chunks retain topic context for embedding.
    """
    tokens = len(enc.encode(chunk["text"]))
    chunk["token_count"] = tokens

    if tokens <= MAX_TOKENS:
        return [chunk]

    # Extract thread title — prepend to every split piece
    thread_name   = chunk.get("thread_name")
    thread_header = f"[Thread: {thread_name}]\n" if thread_name else ""

    lines = chunk["text"].split("\n")
    # Skip existing thread header line — we re-add it to each piece
    if thread_header and lines and lines[0].startswith("[Thread:"):
        lines = lines[1:]

    current, result = [], []
    for line in lines:
        current.append(line)
        full_text = thread_header + "\n".join(current)
        if len(enc.encode(full_text)) > MAX_TOKENS:
            current.pop()
            if current:
                sub_text = thread_header + "\n".join(current)
                sub = {**chunk, "text": sub_text}
                sub["token_count"] = len(enc.encode(sub_text))
                result.append(sub)
            current = [line]

    if current:
        sub_text = thread_header + "\n".join(current)
        sub = {**chunk, "text": sub_text}
        sub["token_count"] = len(enc.encode(sub_text))
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

    print(f"\nReply-aware chunks:  {len(reply_chunks)}")
    print(f"Thread chunks:       {len(thread_chunks)}")
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