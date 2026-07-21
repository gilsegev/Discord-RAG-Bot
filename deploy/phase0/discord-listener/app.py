import asyncio
import hashlib
import logging
import os
from collections import OrderedDict
from typing import Any

import aiohttp
import discord


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ragbot.discord_listener")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("replace_with_"):
        raise RuntimeError(f"{name} must be configured")
    return value


def parse_snowflakes(name: str) -> set[int]:
    raw = os.getenv(name, "")
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise RuntimeError(f"{name} contains a non-numeric Discord ID") from exc
    return values


DISCORD_BOT_TOKEN = required_env("DISCORD_BOT_TOKEN")
AUTHOR_HASH_SALT = required_env("DISCORD_AUTHOR_HASH_SALT")
ALLOWED_GUILD_ID = int(required_env("DISCORD_ALLOWED_GUILD_ID"))
EXCLUDED_CHANNEL_IDS = parse_snowflakes("DISCORD_EXCLUDED_CHANNEL_IDS")
N8N_INTAKE_URL = os.getenv(
    "N8N_INTAKE_URL", "http://n8n:5678/webhook/rag-intake-phase-9"
).strip()
N8N_REQUEST_TIMEOUT_SECONDS = float(os.getenv("N8N_REQUEST_TIMEOUT_SECONDS", "600"))
DELIVERY_QUEUE_SIZE = int(os.getenv("DISCORD_DELIVERY_QUEUE_SIZE", "100"))
SEEN_MESSAGE_CACHE_SIZE = int(os.getenv("DISCORD_SEEN_MESSAGE_CACHE_SIZE", "1000"))
READY_FILE = "/tmp/discord-listener-ready"


def hash_author(author_id: int) -> str:
    material = f"{AUTHOR_HASH_SALT}:{author_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class DiscordListener(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.delivery_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=DELIVERY_QUEUE_SIZE
        )
        self.seen_message_ids: OrderedDict[int, None] = OrderedDict()
        self.http_session: aiohttp.ClientSession | None = None
        self.delivery_worker: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=N8N_REQUEST_TIMEOUT_SECONDS)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        self.delivery_worker = asyncio.create_task(
            self._delivery_loop(), name="n8n-delivery-worker"
        )

    async def close(self) -> None:
        if self.delivery_worker:
            self.delivery_worker.cancel()
            await asyncio.gather(self.delivery_worker, return_exceptions=True)
        if self.http_session:
            await self.http_session.close()
        try:
            os.remove(READY_FILE)
        except FileNotFoundError:
            pass
        await super().close()

    async def on_ready(self) -> None:
        with open(READY_FILE, "w", encoding="utf-8") as ready_file:
            ready_file.write(str(self.user.id if self.user else "ready"))
        logger.info(
            "Discord Gateway ready as bot_user_id=%s guild_id=%s connected_guild_ids=%s excluded_channels=%s",
            self.user.id if self.user else "unknown",
            ALLOWED_GUILD_ID,
            ",".join(str(guild.id) for guild in self.guilds),
            len(EXCLUDED_CHANNEL_IDS),
        )

    async def on_disconnect(self) -> None:
        try:
            os.remove(READY_FILE)
        except FileNotFoundError:
            pass
        logger.warning("Discord Gateway disconnected; discord.py will attempt to resume")

    def _is_excluded_channel(self, message: discord.Message) -> bool:
        channel_id = int(message.channel.id)
        parent_id = getattr(message.channel, "parent_id", None)
        return channel_id in EXCLUDED_CHANNEL_IDS or (
            parent_id is not None and int(parent_id) in EXCLUDED_CHANNEL_IDS
        )

    def _mark_and_check_duplicate(self, message_id: int) -> bool:
        duplicate = message_id in self.seen_message_ids
        self.seen_message_ids[message_id] = None
        self.seen_message_ids.move_to_end(message_id)
        while len(self.seen_message_ids) > SEEN_MESSAGE_CACHE_SIZE:
            self.seen_message_ids.popitem(last=False)
        return duplicate

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != ALLOWED_GUILD_ID:
            return
        if self._is_excluded_channel(message):
            return

        logger.info(
            "Received Discord event message_id=%s channel_id=%s author_is_bot=%s content_length=%s",
            message.id,
            message.channel.id,
            message.author.bot,
            len(message.content or ""),
        )

        duplicate = self._mark_and_check_duplicate(message.id)
        bot_user_id = self.user.id if self.user else None
        direct_mention = bool(
            bot_user_id and any(user.id == bot_user_id for user in message.mentions)
        )
        is_system_event = message.type not in {
            discord.MessageType.default,
            discord.MessageType.reply,
            discord.MessageType.thread_starter_message,
        }

        event = {
            "trigger_source": "discord_active" if direct_mention else "discord_passive",
            "discord_message_id": str(message.id),
            "guild_id": str(message.guild.id),
            "channel_id": str(message.channel.id),
            "channel_name": getattr(message.channel, "name", ""),
            "author_id_hash": hash_author(message.author.id),
            "user_query": message.content or "",
            "author_is_bot": bool(message.author.bot),
            "is_webhook": message.webhook_id is not None,
            "is_system_event": is_system_event,
            "is_duplicate": duplicate,
            "is_direct_mention": direct_mention,
            "passive_enabled": True,
            "allow_discord_post": direct_mention,
            "requested_by": "bot",
        }

        try:
            self.delivery_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.error(
                "Dropping Discord event because delivery queue is full message_id=%s channel_id=%s",
                message.id,
                message.channel.id,
            )

    async def _delivery_loop(self) -> None:
        while True:
            event = await self.delivery_queue.get()
            try:
                await self._deliver(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to deliver Discord event message_id=%s channel_id=%s",
                    event.get("discord_message_id"),
                    event.get("channel_id"),
                )
            finally:
                self.delivery_queue.task_done()

    async def _deliver(self, event: dict[str, Any]) -> None:
        if self.http_session is None:
            raise RuntimeError("HTTP session is not initialized")
        async with self.http_session.post(N8N_INTAKE_URL, json=event) as response:
            response_body = await response.text()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"n8n intake returned HTTP {response.status}: {response_body[:300]}"
                )
            route_type = "unknown"
            try:
                result = await response.json(content_type=None)
                route_type = result.get("route_type", result.get("final_status", "unknown"))
            except (ValueError, TypeError):
                pass
            logger.info(
                "Delivered Discord event message_id=%s channel_id=%s route=%s",
                event.get("discord_message_id"),
                event.get("channel_id"),
                route_type,
            )


if __name__ == "__main__":
    client = DiscordListener()
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
