import importlib
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("DISCORD_AUTHOR_HASH_SALT", "phase10-test-salt")
os.environ.setdefault("DISCORD_ALLOWED_GUILD_ID", "123")
sys.modules.pop("app", None)
app = importlib.import_module("app")


class FeedbackEventTests(unittest.TestCase):
    def listener(self):
        listener = Mock()
        listener.user = SimpleNamespace(id=999)
        listener._is_excluded_channel_id.return_value = False
        return listener

    def payload(self, emoji="👍", user_id=42, guild_id=123, member=None):
        return SimpleNamespace(guild_id=guild_id, channel_id=456, message_id=789,
                               user_id=user_id, emoji=emoji, member=member)

    def test_normalizes_positive_reaction(self):
        event = app.DiscordListener._feedback_event(
            self.listener(), self.payload(), "reaction_added")
        self.assertEqual(event["feedback_type"], "positive")
        self.assertEqual(event["feedback_value"], "thumbs_up")
        self.assertEqual(len(event["feedback_author_id_hash"]), 64)
        self.assertNotIn("user_id", event)

    def test_normalizes_skin_tone_positive_reaction(self):
        event = app.DiscordListener._feedback_event(
            self.listener(), self.payload("👍🏻"), "reaction_added")
        self.assertEqual(event["feedback_type"], "positive")
        self.assertEqual(event["feedback_value"], "thumbs_up")
        self.assertEqual(event["reaction_name"], "👍🏻")

    def test_normalizes_negative_removal(self):
        event = app.DiscordListener._feedback_event(
            self.listener(), self.payload("👎"), "reaction_removed")
        self.assertEqual(event["feedback_type"], "negative")
        self.assertEqual(event["event_kind"], "reaction_removed")

    def test_ignores_unsupported_reaction(self):
        self.assertIsNone(app.DiscordListener._feedback_event(
            self.listener(), self.payload("🎉"), "reaction_added"))

    def test_ignores_other_guild(self):
        self.assertIsNone(app.DiscordListener._feedback_event(
            self.listener(), self.payload(guild_id=321), "reaction_added"))

    def test_ignores_bot_reaction(self):
        self.assertIsNone(app.DiscordListener._feedback_event(
            self.listener(), self.payload(user_id=999), "reaction_added"))

    def test_ignores_other_bot_reaction(self):
        self.assertIsNone(app.DiscordListener._feedback_event(
            self.listener(), self.payload(member=SimpleNamespace(bot=True)),
            "reaction_added"))


class MessageCaptureEnvelopeTests(unittest.TestCase):
    def listener(self):
        listener = Mock()
        listener.user = SimpleNamespace(id=999)
        return listener

    def message(self, *, thread=False, reply=False, direct_mention=False):
        parent = SimpleNamespace(id=456, name="general") if thread else None
        channel = SimpleNamespace(
            id=789 if thread else 456,
            name="a-thread" if thread else "general",
            parent_id=parent.id if parent else None,
            parent=parent,
        )
        message_type = (
            app.discord.MessageType.reply
            if reply else app.discord.MessageType.default
        )
        return SimpleNamespace(
            id=1234,
            guild=SimpleNamespace(id=123),
            channel=channel,
            author=SimpleNamespace(id=42, name="member-name", bot=False),
            mentions=[SimpleNamespace(id=999)] if direct_mention else [],
            reference=SimpleNamespace(message_id=1111) if reply else None,
            created_at=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
            type=message_type,
            attachments=[SimpleNamespace(id=1)],
            webhook_id=None,
            content="How should I prepare?",
        )

    def test_normal_channel_capture_envelope(self):
        event = app.DiscordListener._message_event(
            self.listener(), self.message(), duplicate=False)
        self.assertTrue(event["capture_candidate"])
        self.assertEqual(event["channel_id"], "456")
        self.assertEqual(event["parent_channel_id"], "456")
        self.assertEqual(event["parent_channel_name"], "general")
        self.assertIsNone(event["thread_id"])
        self.assertIsNone(event["thread_name"])
        self.assertIsNone(event["parent_message_id"])
        self.assertEqual(event["author_display_name"], "member-name")
        self.assertEqual(event["message_created_at"], "2026-07-25T12:00:00+00:00")
        self.assertTrue(event["has_attachments"])
        self.assertFalse(event["is_system_event"])

    def test_thread_reply_capture_envelope(self):
        event = app.DiscordListener._message_event(
            self.listener(),
            self.message(thread=True, reply=True, direct_mention=True),
            duplicate=True,
        )
        self.assertEqual(event["trigger_source"], "discord_active")
        self.assertEqual(event["channel_id"], "789")
        self.assertEqual(event["parent_channel_id"], "456")
        self.assertEqual(event["parent_channel_name"], "general")
        self.assertEqual(event["thread_id"], "789")
        self.assertEqual(event["thread_name"], "a-thread")
        self.assertEqual(event["parent_message_id"], "1111")
        self.assertEqual(event["message_type"], "reply")
        self.assertTrue(event["is_duplicate"])

    def test_thread_starter_is_not_corpus_eligible_message_type(self):
        message = self.message(thread=True)
        message.type = app.discord.MessageType.thread_starter_message
        event = app.DiscordListener._message_event(
            self.listener(), message, duplicate=False)
        self.assertTrue(event["is_system_event"])


class WebhookAuthenticationTests(unittest.TestCase):
    def test_webhook_secret_defaults_to_empty(self):
        self.assertEqual(app.N8N_WEBHOOK_SHARED_SECRET, "")


if __name__ == "__main__":
    unittest.main()
