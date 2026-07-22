import importlib
import os
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
