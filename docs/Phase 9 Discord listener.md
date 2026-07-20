# Phase 9 Discord Gateway Listener

## Purpose

`ragbot-discord-listener` is the dedicated Discord ingress service for Phase 9.
It maintains a Discord Gateway connection, receives `MESSAGE_CREATE` events from
an explicitly configured server and channel allowlist, and forwards normalized
events to the Phase 9 n8n intake workflow.

It is separate from the existing `discord_notifier` container and uses its own
Discord application, bot token, Docker container, and runtime configuration.

```text
Discord Gateway
-> ragbot-discord-listener
-> http://n8n:5678/webhook/rag-intake-phase-9
-> active / passive / ignored routing
-> shared RAG core when eligible
```

The listener does not expose a public port.

## Discord Application Configuration

Create a dedicated Discord application and bot in the Discord Developer Portal.

Enable:

- `Message Content Intent` on the application's **Bot** page

Grant only:

- `View Channels`
- `Read Message History`

For Phase 9 shadow mode, do not grant `Send Messages`. Active-call posting can
continue through the existing configured Discord webhook. Phase 9B should review
the posting mechanism and permissions separately.

Add the bot only to channels whose messages may be processed. The server-side
channel allowlist provides a second enforcement layer.

## Server Configuration

On Oracle:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
```

Add these values to the existing uncommitted `.env` file:

```env
DISCORD_BOT_TOKEN=replace_with_the_dedicated_bot_token
DISCORD_AUTHOR_HASH_SALT=replace_with_a_long_random_secret
DISCORD_ALLOWED_GUILD_ID=replace_with_the_discord_server_id
DISCORD_ALLOWED_CHANNEL_IDS=111111111111111111,222222222222222222
```

Generate the author-hash salt on the server:

```bash
openssl rand -hex 32
```

Do not commit or print the completed `.env` file.

Discord IDs can be copied after enabling **Developer Mode** under Discord's
Advanced settings, then using **Copy Server ID** or **Copy Channel ID**.

The listener fails closed when its token, salt, guild ID, or channel allowlist is
missing. An empty channel allowlist never means "monitor every channel."

## Build And Start

The listener uses a Compose profile so it is not started before secrets and
channel scope are configured.

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose --profile discord-listener build discord-listener
docker compose --profile discord-listener up -d discord-listener
```

Check it:

```bash
docker compose --profile discord-listener ps discord-listener
docker compose --profile discord-listener logs --tail 100 discord-listener
```

A healthy startup logs `Discord Gateway ready` without logging message content or
the bot token.

## Always-On Behavior

The service has:

```yaml
restart: unless-stopped
```

After the first successful `up -d`, Docker restarts it after process failure,
Docker daemon restart, or server reboot. No cron job or systemd unit is needed
beyond the existing Docker service.

If an operator explicitly runs `docker stop ragbot-discord-listener`, it remains
stopped until started again:

```bash
docker compose --profile discord-listener up -d discord-listener
```

## Update And Restart

After pulling listener code changes:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose --profile discord-listener up -d --build discord-listener
```

To change guild/channel configuration, edit `.env`, then recreate the service:

```bash
docker compose --profile discord-listener up -d --force-recreate discord-listener
```

## Stop Or Disable

Emergency stop:

```bash
docker compose --profile discord-listener stop discord-listener
```

Remove the listener container without affecting the rest of the RAG stack:

```bash
docker compose --profile discord-listener rm -f discord-listener
```

Revoking or resetting the dedicated bot token in Discord is the external kill
switch.

## Validation

Send test messages in one allowlisted channel and inspect:

```bash
docker compose --profile discord-listener logs -f discord-listener
docker compose logs -f n8n
```

Expected routing:

| Message | Phase 9 route |
|---|---|
| Direct bot mention | `active_call` |
| Relevant ordinary question | `passive_candidate` |
| Acknowledgement such as `thanks` | `ignored` |
| Bot, webhook, or system message | `ignored` |

For passive candidates, verify `response_status = not_posted` and
`discord_response_message_id IS NULL` in Postgres.

## Operational Notes

- The listener processes one n8n delivery at a time through a bounded in-memory
  queue. This avoids a burst of Discord traffic creating uncontrolled concurrent
  RAG runs.
- The listener caches recently seen message IDs and marks repeat Gateway delivery
  as duplicate. The Phase 9 intake records it as ignored.
- Delivery is intentionally not retried after an ambiguous timeout because the
  current intake does not yet enforce durable idempotency by Discord message ID.
- Message content is not written to listener logs.
- Threads are accepted when either the thread ID or its parent channel ID is in
  the allowlist.
