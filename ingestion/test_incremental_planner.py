import unittest

from ingestion.incremental_planner import (
    PlanningError, WorkItem, coalesce_work, create_shadow_plan, render_plan,
)


def message(mid, minute, parent=None, channel="10", thread=None):
    return {
        "id": str(mid), "channel_id": channel, "channel": f"channel-{channel}",
        "thread_id": thread, "thread_name": None, "parent_id": str(parent) if parent else None,
        "author": "user", "content": f"message {mid} with useful content",
        "timestamp": f"2026-07-28T00:{minute:02d}:00+00:00",
    }


class IncrementalPlannerTests(unittest.TestCase):
    def test_reply_work_coalesces_by_root(self):
        records = [message(1, 0), message(2, 1, 1), message(3, 2, 2)]
        work = [
            WorkItem("2", 1, "reply_conversation", "10", None, "1"),
            WorkItem("3", 2, "reply_conversation", "10", None, "2"),
        ]
        groups = coalesce_work(work, records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["root_message_id"], "1")

    def test_adjacent_windows_coalesce_and_distant_windows_do_not(self):
        records = [message(1, 0), message(2, 10), message(3, 40)]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "recent_window", "10", None, None),
            WorkItem("3", 3, "recent_window", "10", None, None),
        ]
        groups = coalesce_work(work, records)
        self.assertEqual([len(g["source_message_ids"]) for g in groups], [2, 1])

    def test_two_distant_singletons_match_original_chunker_buffering(self):
        records = [message(1, 0), message(2, 40)]
        groups = coalesce_work(
            [
                WorkItem("1", 1, "recent_window", "10", None, None),
                WorkItem("2", 2, "recent_window", "10", None, None),
            ],
            records,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["source_message_ids"], ["1", "2"])

    def test_cycle_fails_closed(self):
        records = [message(1, 0, 2), message(2, 1, 1)]
        with self.assertRaisesRegex(PlanningError, "cycle"):
            coalesce_work(
                [WorkItem("2", 1, "reply_conversation", "10", None, "1")],
                records,
            )

    def test_plan_is_deterministic_and_selects_no_unrelated_points(self):
        records = [message(1, 0), message(2, 1, 1), message(9, 2, channel="99")]
        work = [WorkItem("2", 1, "reply_conversation", "10", None, "1")]
        manifest = [
            {"point_id": "100", "channel_id": "10", "thread_id": None,
             "root_message_id": "1", "message_ids": ["1"], "active": True},
            {"point_id": "999", "channel_id": "99", "thread_id": None,
             "root_message_id": None, "message_ids": ["9"], "active": True},
        ]
        points = [
            ("100", {"channel_id": "10", "message_ids": ["1"],
                     "start_ts": records[0]["timestamp"], "end_ts": records[0]["timestamp"]}),
            ("999", {"channel_id": "99", "message_ids": ["9"],
                     "start_ts": records[2]["timestamp"], "end_ts": records[2]["timestamp"]}),
        ]
        first = create_shadow_plan(work, records, manifest, points)
        second = create_shadow_plan(reversed(work), reversed(records), reversed(manifest), reversed(points))
        self.assertEqual(render_plan(first), render_plan(second))
        self.assertEqual(first["groups"][0]["old_point_ids"], ["100"])
        self.assertNotIn("999", first["groups"][0]["old_point_ids"])

    def test_single_recent_message_is_deferred(self):
        records = [message(1, 0)]
        plan = create_shadow_plan(
            [WorkItem("1", 1, "recent_window", "10", None, None)],
            records, [], [],
        )
        self.assertEqual(plan["groups"][0]["status"], "deferred")
        self.assertEqual(plan["replacement_point_count"], 0)

    def test_old_distant_neighbor_does_not_make_window_ready(self):
        records = [message(1, 0), message(2, 40)]
        plan = create_shadow_plan(
            [WorkItem("2", 1, "recent_window", "10", None, None)],
            records, [], [],
        )
        self.assertEqual(plan["groups"][0]["status"], "deferred")
        self.assertEqual(plan["groups"][0]["selected_message_count"], 1)

    def test_shadow_reply_matches_full_chunker_for_affected_fixture(self):
        records = [
            message(1, 0), message(2, 1, 1), message(3, 2, 2),
            message(9, 3, channel="99"),
        ]
        plan = create_shadow_plan(
            [WorkItem("3", 1, "reply_conversation", "10", None, "2")],
            records, [], [],
        )
        shadow_ids = plan["groups"][0]["replacement_point_ids"]
        full_scope = create_shadow_plan(
            [WorkItem("2", 1, "reply_conversation", "10", None, "1")],
            records[:3], [], [],
        )
        self.assertEqual(
            shadow_ids, full_scope["groups"][0]["replacement_point_ids"]
        )

    def test_output_explicitly_reports_zero_qdrant_mutations(self):
        plan = create_shadow_plan([], [], [], [])
        self.assertEqual(render_plan(plan)["qdrant_mutations"], 0)


if __name__ == "__main__":
    unittest.main()
