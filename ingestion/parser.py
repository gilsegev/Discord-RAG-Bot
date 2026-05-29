"""
ingestion/parser.py
Parses DiscordChatExporter JSON exports into clean records.
Schema confirmed from gilsegev/Discord-RAG-Bot methodology doc.
Author: ThinkInSystems (Hemanth Aragonda)
"""
import json
import re
from pathlib import Path
from datetime import datetime

# ── All TPM Unite channels ────────────────────────────────────
ELIGIBLE_CHANNELS = {
    # General category
    "general", "announcements", "readme", "polls", "intro",
    "referrals", "job-openings", "resume-review", "mentorship",
    "company-hiring-updates", "tpm-events",
    # TPMs Unite category
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

# Anchored pattern — only process DiscordChatExporter format files
# e.g. "TPMs unite - general [938638026156957757].json"
# Fix #5: added ^ anchor to prevent partial path matches
EXPORT_PATTERN = re.compile(r"^.+\[\d+\]\.json$")


def parse_export_file(json_path: str) -> list:
    """
    Parse one DiscordChatExporter JSON file.
    Returns list of clean message records.
    Skips: bots, system events, empty/whitespace/emoji-only content.
    """
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  WARN: Skipping {json_path} — JSON error: {e}")
        return []

    channel_name = data["channel"]["name"].lower()
    channel_id   = data["channel"]["id"]

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
        # (keeps short replies like "ok", "yes" which carry
        #  sentiment/confirmation value in reply chains)
        if len(content) < 3 and not has_attachment:
            continue

        # Clean content — strips Discord syntax, collapses emoji
        cleaned = _clean(content)

        # Fix #7 (whitespace guard): skip if cleaning removed all
        # content including whitespace — handles emoji-only messages
        # like "<:fire:123> <:fire:456>" that become "  " after clean
        if not cleaned.strip() and not has_attachment:
            continue

        records.append({
            "id":             msg["id"],           # dedup key
            "author":         msg["author"]["name"],
            "author_id":      msg["author"]["id"],
            "timestamp":      msg["timestamp"],    # ISO 8601
            "content":        cleaned.strip(),
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
    """
    Strip Discord syntax that adds noise to embeddings.
    URL regex runs before emoji collapse to avoid partial matches.
    """
    text = re.sub(r"<@!?\d+>",      "[mention]", text)
    text = re.sub(r"<#\d+>",        "[channel]", text)
    text = re.sub(r"<@&\d+>",       "[role]",    text)
    text = re.sub(r"<a?:\w+:\d+>",  "[emoji]",   text)
    # URLs stripped before emoji collapse — order matters
    text = re.sub(r"https?://\S+",  "[link]",    text)
    text = re.sub(r"\n{3,}",        "\n\n",      text)
    # Collapse emoji tokens — space preserves word boundaries
    text = re.sub(r"(\[emoji\])+",  " ",         text)
    # Clean up double spaces left behind
    text = re.sub(r"\s+",           " ",         text)
    return text.strip()


def parse_all_exports(export_dir: str) -> list:
    """
    Parse all DiscordChatExporter JSON files in export_dir.
    Fix #5: filters by anchored filename pattern.
    Fix #8: warns clearly when JSON files exist but none match pattern.
    Deduplicates by message id across files.
    Fix #4 (timestamp sort): uses datetime not string comparison.
    """
    all_json = list(Path(export_dir).glob("*.json"))
    matching = [p for p in all_json
                if EXPORT_PATTERN.match(p.name)]

    # Fix #8: helpful warning if files exist but none match pattern
    if all_json and not matching:
        print(f"  WARN: Found {len(all_json)} JSON file(s) in "
              f"{export_dir} but none match DiscordChatExporter "
              f"format [channel_id].json")
        return []

    seen_ids    = set()
    all_records = []

    for path in sorted(matching):
        print(f"\nParsing: {path.name}")
        for r in parse_export_file(str(path)):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                all_records.append(r)

    # Fix #4: sort by datetime object — safer than string sort
    # handles mixed timezone formats across exports
    all_records.sort(
        key=lambda r: datetime.fromisoformat(r["timestamp"])
    )

    print(f"\nTotal: {len(all_records)} messages from "
          f"{len({r['channel'] for r in all_records})} channel(s)")
    return all_records


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    records = parse_all_exports("chat_logs")
    preview = next((r for r in records if r.get("content")), None)
    if preview:
        print("\nFirst record with content:")
        print(f"  Author:    {preview['author']}")
        print(f"  Timestamp: {preview['timestamp'][:10]}")
        print(f"  Channel:   #{preview['channel']}")
        print(f"  Content:   {preview['content'][:80]}...")
    else:
        print("\nNo records with content found")