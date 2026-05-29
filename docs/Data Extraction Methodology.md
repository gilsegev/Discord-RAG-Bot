# Data Extraction and Log Reading Guide

## Data Extraction Protocol

We use the open source `DiscordChatExporter` CLI to extract Discord server history. This tool interacts directly with the Discord API using a dedicated bot token, which avoids manual collection and reduces the risk of account suspensions.

### Recommended export strategy

- Export channel by channel, or
- Export in defined date blocks

Keeping exports segmented helps prevent API timeouts and keeps log files manageable.

### Export command

```bash
dotnet DiscordChatExporter.Cli.dll export \
  -t "BOT_TOKEN" \
  -c CHANNEL_ID \
  -o "./chat_logs/" \
  -f Json \
  --include-threads all
```

### Important requirements

- **Target Output:** Export strictly to `Json` format to preserve the metadata hierarchy.
- **Thread Capture:** Use `--include-threads all` to ensure active, private, and archived threads are included.

## Log File Structure

The exported JSON contains two primary sections:

1. **Metadata**
2. **Messages**

The file begins with a header that maps the environment context. It includes:

- `guild` object: server ID and name
- `channel` object: channel ID, name, category, and topic

This metadata is essential for later indexing and storing context in the database.

## Message Object Anatomy

The `messages` array contains individual chat interactions. When writing ingestion scripts, map the following key fields:

- `id`
  - The unique Discord message ID.
  - Use this as the primary key to prevent duplicate entries.
- `timestamp`
  - ISO 8601 formatted date and time.
- `content`
  - The raw text string sent by the user.
- `author`
  - A nested object containing user data: `id`, `name`, and `isBot`.
  - Filter out messages where `isBot: true` unless system logs are intentionally required.
- `type`
  - Identifies the message kind.
  - Standard messages are `Default`.
  - Replies are `Reply`.
- `reference.messageId`
  - Present when `type` is `Reply`.
  - Use this as `parent_id` in the database to link replies to the original message.
- `mentions`
  - An array of tagged users.
  - Use this as a fallback link if a user tagged the asker without using an official reply.
