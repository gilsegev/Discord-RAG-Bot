import unittest

from ingestion.chunker import chunk_records
from ingestion.incremental_planner import WorkItem, create_shadow_plan
from ingestion.run import _stable_id


def message(
    message_id,
    minute,
    *,
    channel_id,
    channel="general",
    parent_id=None,
    thread_name=None,
):
    return {
        "id": str(message_id),
        "author": "user",
        "timestamp": f"2026-09-13T00:{minute:02d}:00+00:00",
        "content": f"useful message {message_id}",
        "channel": channel,
        "channel_id": str(channel_id),
        "thread_name": thread_name,
        "parent_id": str(parent_id) if parent_id is not None else None,
    }


class StableChannelGroupingTests(unittest.TestCase):
    def test_same_display_name_never_combines_distinct_channel_ids(self):
        records = [
            message(1, 0, channel_id=10),
            message(2, 1, channel_id=10),
            message(3, 2, channel_id=20),
            message(4, 3, channel_id=20),
        ]

        chunks = chunk_records(records)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            {(chunk["channel_id"], tuple(chunk["message_ids"])) for chunk in chunks},
            {("10", ("1", "2")), ("20", ("3", "4"))},
        )

    def test_display_name_changes_do_not_split_one_channel_id(self):
        records = [
            message(1, 0, channel_id=10, channel="general"),
            message(2, 1, channel_id=10, channel="renamed-general"),
        ]

        chunks = chunk_records(records)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["message_ids"], ["1", "2"])

    def test_cross_channel_reply_root_is_not_added_to_chunk(self):
        records = [
            message(1, 0, channel_id=10),
            message(2, 1, channel_id=20, parent_id=1),
            message(3, 2, channel_id=20),
        ]

        chunks = chunk_records(records)

        channel_20 = next(chunk for chunk in chunks if chunk["channel_id"] == "20")
        self.assertEqual(channel_20["message_ids"], ["2", "3"])
        self.assertNotIn("1", channel_20["message_ids"])

    def test_full_and_incremental_grouping_agree_for_duplicate_names(self):
        records = [
            message(1, 0, channel_id=10),
            message(2, 1, channel_id=10),
            message(3, 0, channel_id=20),
            message(4, 1, channel_id=20),
        ]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "recent_window", "10", None, None),
            WorkItem("3", 3, "recent_window", "20", None, None),
            WorkItem("4", 4, "recent_window", "20", None, None),
        ]

        full_membership = {
            (chunk["channel_id"], tuple(chunk["message_ids"]))
            for chunk in chunk_records(records)
        }
        plan = create_shadow_plan(work, records, [], [])
        incremental_membership = {
            (row["channel_id"], tuple(row["message_ids"]))
            for rows in plan["_replacement_details"].values()
            for row in rows
        }

        self.assertEqual(incremental_membership, full_membership)

    def test_incremental_rejects_work_item_channel_mismatch(self):
        records = [message(1, 0, channel_id=10)]
        work = [WorkItem("1", 1, "recent_window", "20", None, None)]

        with self.assertRaisesRegex(ValueError, "does not match source record"):
            create_shadow_plan(work, records, [], [])

    def test_incremental_cross_channel_replies_fall_back_to_same_channel_window(self):
        records = [
            message(1, 0, channel_id=10),
            message(2, 1, channel_id=10),
            message(3, 2, channel_id=20, parent_id=1),
            message(4, 3, channel_id=20, parent_id=2),
        ]
        work = [
            WorkItem("3", 1, "reply_conversation", "20", None, "1"),
            WorkItem("4", 2, "reply_conversation", "20", None, "2"),
        ]

        full = chunk_records(records)
        full_channel_20 = next(
            chunk for chunk in full if chunk["channel_id"] == "20"
        )
        plan = create_shadow_plan(work, records, [], [])
        incremental_rows = [
            row
            for rows in plan["_replacement_details"].values()
            for row in rows
        ]

        self.assertEqual(len(plan["groups"]), 1)
        self.assertEqual(plan["groups"][0]["work_kind"], "recent_window")
        self.assertEqual(len(incremental_rows), 1)
        self.assertEqual(
            incremental_rows[0]["message_ids"], full_channel_20["message_ids"]
        )

    def test_new_reply_replaces_window_that_owned_the_new_root(self):
        baseline = [
            message(1, 0, channel_id=10),
            message(2, 1, channel_id=10),
        ]
        old_chunk = chunk_records(baseline)[0]
        old_id = str(_stable_id(old_chunk))
        records = [*baseline, message(3, 2, channel_id=10, parent_id=1)]
        manifest = [{
            "point_id": old_id,
            "channel_id": "10",
            "thread_id": None,
            "root_message_id": None,
            "message_ids": ["1", "2"],
            "active": True,
        }]

        plan = create_shadow_plan(
            [WorkItem("3", 1, "reply_conversation", "10", None, "1")],
            records,
            manifest,
            [(old_id, old_chunk)],
        )

        self.assertEqual(plan["groups"][0]["old_point_ids"], [old_id])

    def test_window_replacement_matches_full_corpus_across_overlap_chain(self):
        def timed(message_id, timestamp):
            record = message(message_id, 0, channel_id=10)
            record["timestamp"] = timestamp
            return record

        baseline = [
            timed(1, "2026-09-13T01:00:00+00:00"),
            timed(2, "2026-09-13T01:40:00+00:00"),
            timed(3, "2026-09-13T02:20:00+00:00"),
            timed(4, "2026-09-13T02:46:00+00:00"),
            timed(7, "2026-09-13T02:51:00+00:00"),
            timed(8, "2026-09-13T02:52:00+00:00"),
        ]
        new_message = timed(9, "2026-09-13T02:53:00+00:00")
        baseline_chunks = chunk_records(baseline)
        points = [(str(_stable_id(chunk)), chunk) for chunk in baseline_chunks]
        manifest = [{
            "point_id": point_id,
            "channel_id": "10",
            "thread_id": None,
            "root_message_id": None,
            "message_ids": chunk["message_ids"],
            "active": True,
        } for point_id, chunk in points]

        plan = create_shadow_plan(
            [WorkItem("9", 1, "recent_window", "10", None, None)],
            [*baseline, new_message],
            manifest,
            points,
        )
        old_ids = {point_id for point_id, _ in points}
        replaced_ids = {
            point_id
            for group in plan["groups"]
            for point_id in group["old_point_ids"]
        }
        replacement_ids = {
            point_id
            for group in plan["groups"]
            for point_id in group["replacement_point_ids"]
        }
        predicted_final = (old_ids - replaced_ids) | replacement_ids
        full_final = {
            str(_stable_id(chunk))
            for chunk in chunk_records([*baseline, new_message])
        }

        self.assertEqual(predicted_final, full_final)


if __name__ == "__main__":
    unittest.main()
