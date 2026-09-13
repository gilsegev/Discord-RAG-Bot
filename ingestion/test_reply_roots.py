import unittest

from ingestion.chunk_manifest import create_plan
from ingestion.chunker import MAX_TOKENS, _build, _split_if_needed, chunk_records, enc
from ingestion.incremental_planner import WorkItem, coalesce_work, create_shadow_plan
from ingestion.reply_roots import resolve_reply_root
from ingestion.run import _stable_id


def message(mid, minute, parent=None, channel_id="10", channel="general", content=None):
    return {
        "id": str(mid),
        "channel_id": str(channel_id),
        "channel": channel,
        "thread_id": None,
        "thread_name": None,
        "parent_id": str(parent) if parent is not None else None,
        "author": "user",
        "content": content or f"message {mid}",
        "timestamp": f"2026-07-28T00:{minute:02d}:00+00:00",
    }


class ReplyRootTests(unittest.TestCase):
    def test_same_display_name_channels_never_share_a_chunk(self):
        records = [
            message(1, 0, channel_id="10"),
            message(2, 1, channel_id="10"),
            message(3, 2, channel_id="20"),
            message(4, 3, channel_id="20"),
        ]
        chunks = chunk_records(records)
        index = {record["id"]: record for record in records}
        for chunk in chunks:
            self.assertEqual(
                {index[mid]["channel_id"] for mid in chunk["message_ids"]},
                {chunk["channel_id"]},
            )

    def test_direct_and_nested_replies_share_root(self):
        records = {r["id"]: r for r in [message(1, 0), message(2, 1, 1), message(3, 2, 2)]}
        self.assertEqual(resolve_reply_root("2", records, "10").root_message_id, "1")
        self.assertEqual(resolve_reply_root("3", records, "10").root_message_id, "1")

    def test_missing_immediate_and_higher_ancestors_fail_closed(self):
        immediate = {"2": message(2, 1, 1)}
        higher = {"2": message(2, 1, 1), "3": message(3, 2, 2)}
        self.assertEqual(resolve_reply_root("2", immediate, "10").failure_reason, "missing_immediate_parent")
        self.assertEqual(resolve_reply_root("3", higher, "10").failure_reason, "missing_higher_ancestor")

    def test_self_and_multi_message_cycles_fail_from_every_start(self):
        self_cycle = {"1": message(1, 0, 1)}
        self.assertEqual(resolve_reply_root("1", self_cycle, "10").failure_reason, "cycle")
        cycle = {r["id"]: r for r in [message(1, 0, 2), message(2, 1, 3), message(3, 2, 1)]}
        for mid in cycle:
            result = resolve_reply_root(mid, cycle, "10")
            self.assertIsNone(result.root_message_id)
            self.assertEqual(result.failure_reason, "cycle")

    def test_cross_channel_parent_fails_closed(self):
        records = {"1": message(1, 0, channel_id="20"), "2": message(2, 1, 1, channel_id="10")}
        result = resolve_reply_root("2", records, "10")
        self.assertIsNone(result.root_message_id)
        self.assertEqual(result.failure_reason, "cross_channel")

    def test_unresolved_singleton_remains_searchable(self):
        chunks = chunk_records([message(2, 1, 1)])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["message_ids"], ["2"])
        self.assertIsNone(chunks[0]["root_message_id"])

    def test_split_pieces_preserve_trusted_root(self):
        records = [
            message(i, i, 1 if i > 1 else None, content="reply " * 80)
            for i in range(1, 13)
        ]
        chunk = _build(records, root_message_id="1")
        self.assertGreater(len(enc.encode(chunk["text"])), MAX_TOKENS)
        pieces = _split_if_needed(chunk, {r["id"]: r for r in records})
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(piece["root_message_id"] == "1" for piece in pieces))

    def test_full_manifest_and_incremental_roots_are_consistent(self):
        records = [message(1, 0), message(2, 1, 1), message(3, 2, 2)]
        chunk = chunk_records(records)[0]
        payload = {
            **chunk,
            "first_message_id": chunk["first_message_id"],
            "split_index": chunk["split_index"],
        }
        manifest = create_plan(
            [(str(_stable_id(chunk)), payload)], records, "c", "v11", "embed"
        )
        incremental = coalesce_work(
            [WorkItem("3", 1, "reply_conversation", "10", None, "2")], records
        )
        self.assertEqual(chunk["root_message_id"], "1")
        self.assertEqual(manifest["rows"][0]["root_message_id"], "1")
        self.assertEqual(incremental[0]["root_message_id"], "1")

    def test_thread_display_name_does_not_change_manifest_root(self):
        records = [message(1, 0), message(2, 1, 1)]
        records[0]["thread_name"] = "old name"
        records[1]["thread_name"] = "new name"
        chunk = chunk_records(records)[0]
        payload = {**chunk, "thread_name": "payload name"}
        plan = create_plan(
            [(str(_stable_id(chunk)), payload)], records, "c", "v11", "embed"
        )
        self.assertEqual(plan["rows"][0]["root_message_id"], "1")
        self.assertIsNone(plan["rows"][0]["thread_id"])

    def test_incremental_forum_threads_use_distinct_stable_channel_ids(self):
        records = [
            message(1, 0, channel_id="thread-a"),
            message(2, 1, channel_id="thread-a"),
            message(3, 2, channel_id="thread-b"),
            message(4, 3, channel_id="thread-b"),
        ]
        work = [
            WorkItem("1", 1, "recent_window", "thread-a", "thread-a", None),
            WorkItem("2", 2, "recent_window", "thread-a", "thread-a", None),
            WorkItem("3", 3, "recent_window", "thread-b", "thread-b", None),
            WorkItem("4", 4, "recent_window", "thread-b", "thread-b", None),
        ]
        groups = coalesce_work(work, records)
        self.assertEqual({group["channel_id"] for group in groups}, {"thread-a", "thread-b"})
        self.assertTrue(all(group["thread_id"] is None for group in groups))

    def test_new_descendant_and_late_ancestor_repair_use_existing_root(self):
        records = [message(1, 0), message(2, 1, 1), message(3, 2, 2)]
        descendant = coalesce_work(
            [WorkItem("3", 1, "reply_conversation", "10", None, "2")], records
        )
        self.assertEqual(descendant[0]["root_message_id"], "1")

        repaired = coalesce_work(
            [WorkItem("1", 2, "recent_window", "10", None, None)], records
        )
        self.assertEqual(repaired[0]["work_kind"], "reply_conversation")
        self.assertEqual(repaired[0]["root_message_id"], "1")

        fallback_chunk = chunk_records([message(2, 1, 1), message(3, 2, 2)])[0]
        fallback_point = str(_stable_id(fallback_chunk))
        repair_plan = create_shadow_plan(
            [WorkItem("1", 2, "recent_window", "10", None, None)],
            records,
            [{
                "point_id": fallback_point,
                "channel_id": "10",
                "thread_id": None,
                "root_message_id": None,
                "message_ids": ["2", "3"],
                "active": True,
            }],
            [(fallback_point, fallback_chunk)],
        )
        self.assertIn(fallback_point, repair_plan["groups"][0]["old_point_ids"])

    def test_incremental_payload_contains_nullable_root(self):
        records = [message(1, 0), message(2, 1, 1)]
        plan = create_shadow_plan(
            [WorkItem("2", 1, "reply_conversation", "10", None, "1")],
            records,
            [],
            [],
        )
        rows = next(iter(plan["_replacement_details"].values()))
        self.assertEqual(rows[0]["_payload"]["root_message_id"], "1")
        self.assertEqual(plan["chunker_version"], "v11")


if __name__ == "__main__":
    unittest.main()
