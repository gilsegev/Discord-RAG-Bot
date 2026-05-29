"""
ingestion/parser.py
Parses DiscordChatExporter JSON exports into clean records.
Schema confirmed from gilsegev/Discord-RAG-Bot methodology doc.
Author: ThinkInSystems (Hemanth Aragonda)
"""
import json
import re
from pathlib import Path

# ── All TPM Unite channels ────────────────────────────────────
ELIGIBLE_CHANNELS = {
    # General category
    "general", "announcements", "readme", "polls", "intro",
    "referrals", "job-openings", "resume-review", "mentorship",
    "company-hiring-updates", "tpm-events",
    # TPM's Unite category
    "tpm-interview-resources", "new-grads", "tc-sharing",
    "hardware-tpms", "tpm-tradecraft", "tpm-stories",
    "system-design", "mock-interviews", "interview-experience",
    "monthly-tpm-chats", "layoffs", "tpm-leadership",
    "ai-for-tpms", "infra-tpms", "rag-bot-community-project",
    # Product Managers Unite category
    "pm-intro", "pm-interview-resources",
    "prod-manager-interview-experience",
    # Major Companies category
    "amazon", "netflix", "meta", "google", "apple", "microsoft",
    # Offtopic category
    "offtopic", "investments-and-finances",
    # Uncategorized
    "forum-discussion", "rules",
}


def parse_export_file(json_path: str) -> list:
    """
    Parse one DiscordChatExporter JSON file.
    Returns list of clean message records.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    channel_name = data["channel"]["name"].lower()
    channel_id   = data["channel"]["id"]

    # Skip channels not in the list
    if channel_name not in ELIGIBLE_CHANNELS:
        print(f"  SKIP: #{channel_name} (not in channel list)")
        return []

    records = []
    for msg in data["messages"]:

        # Only keep real messages — skip system events
        if msg.get("type") not in ("Default", "Reply"):
            continue

        # Skip bots (isBot confirmed in methodology doc)
        if msg["author"].get("isBot", False):
            continue

        content        = msg.get("content", "").strip()
        has_attachment = bool(msg.get("attachments"))

        # Skip empty messages with no attachment
        if not content and not has_attachment:
            continue

        # Skip very short noise — under 3 characters
        if len(content) < 3 and not has_attachment:
            continue

        # Clean content and skip if nothing remains after cleaning
        # (handles emoji-only messages that become empty after stripping)
        cleaned = _clean(content)
        if not cleaned and not has_attachment:
            continue

        records.append({
            "id":             msg["id"],           # dedup key
            "author":         msg["author"]["name"],
            "author_id":      msg["author"]["id"],
            "timestamp":      msg["timestamp"],    # ISO 8601
            "content":        cleaned,
            "channel":        channel_name,
            "channel_id":     channel_id,
            # reply chain — reference.messageId from methodology doc
            "parent_id":      msg.get("reference", {}).get("messageId"),
            "mentions":       [m["name"] for m in msg.get("mentions", [])],
            "has_attachment": has_attachment,
        })

    print(f"  OK: #{channel_name}: {len(records)} messages parsed")
    return records


def _clean(text: str) -> str:
    """Strip Discord syntax that adds noise to embeddings."""
    text = re.sub(r"<@!?\d+>",      "[mention]", text)
    text = re.sub(r"<#\d+>",        "[channel]", text)
    text = re.sub(r"<@&\d+>",       "[role]",    text)
    text = re.sub(r"<a?:\w+:\d+>",  "[emoji]",   text)
    text = re.sub(r"https?://\S+",  "[link]",    text)
    text = re.sub(r"\n{3,}",        "\n\n",      text)
    # Collapse emoji tokens — replace with space to preserve word boundaries
    text = re.sub(r"(\[emoji\])+",  " ",         text)
    # Clean up any double spaces left behind
    text = re.sub(r"\s+",           " ",         text)
    return text.strip()


def parse_all_exports(export_dir: str) -> list:
    """
    Parse all JSON files in the export directory.
    Deduplicates by message id across files.
    """
    seen_ids    = set()
    all_records = []

    for path in sorted(Path(export_dir).glob("*.json")):
        print(f"\nParsing: {path.name}")
        for r in parse_export_file(str(path)):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                all_records.append(r)

    # Sort chronologically
    all_records.sort(key=lambda r: r["timestamp"])

    print(f"\nTotal: {len(all_records)} messages from "
          f"{len({r['channel'] for r in all_records})} channel(s)")
    return all_records


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    records = parse_all_exports("chat_logs")
    # Find first record with actual content for preview
    preview = next((r for r in records if r.get("content")), None)
    if preview:
        print("\nFirst record with content:")
        print(f"  Author:    {preview['author']}")
        print(f"  Timestamp: {preview['timestamp'][:10]}")
        print(f"  Channel:   #{preview['channel']}")
        print(f"  Content:   {preview['content'][:80]}...")
    else:
        print("\nNo records with content found")