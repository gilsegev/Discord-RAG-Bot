"""
ingestion/chunker.py
Groups messages into 15-minute conversation windows.
- Channel-level grouping (never mixes channels)
- 2-message overlap at boundaries
- Token budget validation (max 700 tokens)
Author: ThinkInSystems (Hemanth Aragonda)
"""
import tiktoken
from datetime import datetime

enc          = tiktoken.get_encoding("cl100k_base")
WINDOW_MINS  = 15   # new chunk if gap > 15 minutes
MIN_MSGS     = 2    # skip chunks with fewer than 2 messages
MAX_TOKENS   = 700  # split if chunk exceeds this
OVERLAP_MSGS = 2    # carry last N msgs into next chunk


def chunk_records(records: list) -> list:
    """Main entry point. Returns chunks ready for embedding."""

    # Group by channel — never mix channels in one chunk
    by_channel = {}
    for r in records:
        by_channel.setdefault(r["channel"], []).append(r)

    all_chunks = []
    for channel, msgs in by_channel.items():
        msgs = sorted(msgs, key=lambda m: m["timestamp"])
        for chunk in _window_chunk(msgs):
            all_chunks.extend(_split_if_needed(chunk))

    # Sort all chunks chronologically
    all_chunks.sort(key=lambda c: c["start_ts"])

    print(f"Created {len(all_chunks)} chunks from "
          f"{len(records)} messages across "
          f"{len(by_channel)} channel(s)")
    return all_chunks


def _window_chunk(msgs: list) -> list:
    """Split one channel's messages into 15-minute windows."""
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


def _build(msgs: list) -> dict:
    """Format a list of messages into one chunk dict."""
    lines = []
    for m in msgs:
        date = m["timestamp"][:10]
        line = f"[{m['author']} @ {date}]: {m['content']}"
        if m.get("parent_id"):
            line = "  > " + line  # indent replies
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


def _split_if_needed(chunk: dict) -> list:
    """Split chunk at line boundaries if it exceeds MAX_TOKENS."""
    tokens = len(enc.encode(chunk["text"]))
    chunk["token_count"] = tokens

    if tokens <= MAX_TOKENS:
        return [chunk]

    # Split at line boundaries — never mid-message
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
    print(f"\nFirst chunk preview:")
    print(f"  Channel:  #{chunks[0]['channel']}")
    print(f"  Date:     {chunks[0]['start_ts'][:10]}")
    print(f"  Messages: {chunks[0]['message_count']}")
    print(f"  Tokens:   {chunks[0]['token_count']}")
    print(f"  Authors:  {', '.join(chunks[0]['authors'])}")
    print(f"\n  Text preview:")
    print(f"  {chunks[0]['text'][:300]}...")