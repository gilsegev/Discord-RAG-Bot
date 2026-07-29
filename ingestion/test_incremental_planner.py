import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ingestion.incremental_planner import (
    PlanningError, WorkItem, _validate_existing_groups,
    _validate_status_transition, coalesce_work, create_shadow_plan, render_plan,
)


def message(mid, minute, parent=None, channel="10", thread=None):
    return {
        "id": str(mid), "channel_id": channel, "channel": f"channel-{channel}",
        "thread_id": thread, "thread_name": None, "parent_id": str(parent) if parent else None,
        "author": "user", "content": f"message {mid} with useful content",
        "timestamp": f"2026-07-28T00:{minute:02d}:00+00:00",
    }


SOURCE_CORPUS = {
    "corpus_version_id": "corpus-fixture",
    "manifest_digest": "a" * 64,
}
def complete_measurement(plan):
    return {
        "measurement_kind": "shadow_replacements",
        "embedded_chunk_count": plan["replacement_point_count"],
        "embedding_dimensions": [768],
        "observed_embedding_models": [plan["embedding_version"]],
        "measured_embedding_seconds": 0.1,
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

    def test_same_batch_late_reply_absorbs_root_window_work(self):
        records = [message(1, 0), message(2, 40, 1)]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "reply_conversation", "10", None, "1"),
        ]
        groups = coalesce_work(work, records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["work_kind"], "reply_conversation")
        self.assertEqual(groups[0]["root_message_id"], "1")
        self.assertEqual(groups[0]["source_message_ids"], ["1", "2"])

        plan = create_shadow_plan(work, records, [], [])
        self.assertEqual(plan["ready_group_count"] if "ready_group_count" in plan else sum(
            group["status"] == "ready" for group in plan["groups"]
        ), 1)
        self.assertEqual(plan["replacement_point_count"], 1)

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

    def test_ready_plan_without_complete_evidence_remains_planned(self):
        records = [message(1, 0), message(2, 1)]
        plan = create_shadow_plan(
            [
                WorkItem("1", 1, "recent_window", "10", None, None),
                WorkItem("2", 2, "recent_window", "10", None, None),
            ],
            records,
            [],
            [],
            source_corpus=SOURCE_CORPUS,
        )
        rendered = render_plan(
            plan,
            source_corpus_current=True,
        )
        self.assertEqual(rendered["validation"]["status"], "planned")
        self.assertIn(
            "production replacement embedding evidence",
            rendered["validation"]["missing_evidence"],
        )

    def test_complete_evidence_is_required_for_shadow_validation(self):
        records = [message(1, 0), message(2, 1)]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "recent_window", "10", None, None),
        ]
        plan = create_shadow_plan(
            work, records, [], [], source_corpus=SOURCE_CORPUS
        )
        rendered = render_plan(
            plan,
            complete_measurement(plan),
            source_corpus_current=True,
        )
        self.assertEqual(rendered["validation"]["status"], "shadow_validated")
        self.assertTrue(all(rendered["validation"]["checks"].values()))

    def test_embedding_contradictions_fail_closed(self):
        records = [message(1, 0), message(2, 1)]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "recent_window", "10", None, None),
        ]
        plan = create_shadow_plan(
            work, records, [], [], source_corpus=SOURCE_CORPUS
        )
        measurement = complete_measurement(plan)
        measurement["embedding_dimensions"] = [384]
        rendered = render_plan(
            plan,
            measurement,
            source_corpus_current=True,
        )
        self.assertEqual(rendered["validation"]["status"], "failed")
        self.assertIn(
            "embedding dimensions are not exactly 768",
            rendered["validation"]["contradictions"],
        )

    def test_embedding_count_must_cover_every_replacement(self):
        records = [message(1, 0), message(2, 1)]
        plan = create_shadow_plan(
            [
                WorkItem("1", 1, "recent_window", "10", None, None),
                WorkItem("2", 2, "recent_window", "10", None, None),
            ],
            records,
            [],
            [],
            source_corpus=SOURCE_CORPUS,
        )
        measurement = complete_measurement(plan)
        measurement["embedded_chunk_count"] -= 1
        rendered = render_plan(
            plan,
            measurement,
            source_corpus_current=True,
        )
        self.assertEqual(rendered["validation"]["status"], "failed")
        self.assertIn(
            "embedded count does not equal replacement count",
            rendered["validation"]["contradictions"],
        )

    def test_observed_embedding_model_must_match_declared_version(self):
        records = [message(1, 0), message(2, 1)]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "recent_window", "10", None, None),
        ]
        plan = create_shadow_plan(
            work, records, [], [], source_corpus=SOURCE_CORPUS
        )
        measurement = complete_measurement(plan)
        measurement["observed_embedding_models"] = ["wrong/model"]
        rendered = render_plan(
            plan,
            measurement,
            source_corpus_current=True,
        )
        self.assertEqual(rendered["validation"]["status"], "failed")
        self.assertIn(
            "observed embedding model/version mismatch",
            rendered["validation"]["contradictions"],
        )

    def test_stale_source_corpus_fails_closed(self):
        records = [message(1, 0), message(2, 1)]
        work = [
            WorkItem("1", 1, "recent_window", "10", None, None),
            WorkItem("2", 2, "recent_window", "10", None, None),
        ]
        plan = create_shadow_plan(
            work, records, [], [], source_corpus=SOURCE_CORPUS
        )
        rendered = render_plan(
            plan,
            complete_measurement(plan),
            source_corpus_current=False,
        )
        self.assertEqual(rendered["validation"]["status"], "failed")
        self.assertIn(
            "source corpus is stale", rendered["validation"]["contradictions"]
        )

    def test_deferred_only_batch_remains_deferred(self):
        records = [message(1, 0)]
        plan = create_shadow_plan(
            [WorkItem("1", 1, "recent_window", "10", None, None)],
            records,
            [],
            [],
            source_corpus=SOURCE_CORPUS,
        )
        rendered = render_plan(
            plan,
            source_corpus_current=True,
        )
        self.assertEqual(rendered["validation"]["status"], "deferred")

    def test_terminal_plan_status_cannot_be_downgraded(self):
        with self.assertRaisesRegex(PlanningError, "invalid persisted"):
            _validate_status_transition("failed", "planned")
        _validate_status_transition("failed", "failed")

    def test_shadow_validated_plan_cannot_lose_evidence(self):
        with self.assertRaisesRegex(PlanningError, "invalid persisted"):
            _validate_status_transition("shadow_validated", "planned")

    def test_existing_persisted_groups_must_match_immutable_evidence(self):
        group = {
            "group_key": "window:10:-:2026-07-28T00:00:00+00:00",
            "work_kind": "recent_window",
            "channel_id": "10",
            "thread_id": None,
            "root_message_id": None,
            "source_message_ids": ["1", "2"],
            "old_point_ids": [],
            "replacement_point_ids": ["123"],
            "status": "ready",
            "selected_message_count": 2,
            "replacement_points": [],
        }
        row = (
            group["group_key"],
            group["work_kind"],
            group["channel_id"],
            group["thread_id"],
            group["root_message_id"],
            group["source_message_ids"],
            group["old_point_ids"],
            group["replacement_point_ids"],
            group["status"],
            group,
        )
        _validate_existing_groups("plan", [row], [group])
        with self.assertRaisesRegex(PlanningError, "immutable evidence"):
            _validate_existing_groups(
                "plan",
                [(*row[:-1], {**group, "selected_message_count": 99})],
                [group],
            )
        with self.assertRaisesRegex(PlanningError, "different group set"):
            _validate_existing_groups("plan", [], [group])

    def test_simulation_input_produces_no_write_review_artifact(self):
        fixture = {
            "work": [
                {
                    "message_id": "1",
                    "capture_sequence": 1,
                    "work_kind": "recent_window",
                    "channel_id": "10",
                    "thread_id": None,
                    "parent_message_id": None,
                },
                {
                    "message_id": "2",
                    "capture_sequence": 2,
                    "work_kind": "recent_window",
                    "channel_id": "10",
                    "thread_id": None,
                    "parent_message_id": None,
                },
            ],
            "records": [message(1, 0), message(2, 1)],
            "manifest": [],
            "points": [],
            "source_corpus": SOURCE_CORPUS,
        }
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "simulation.json"
            output_path = Path(directory) / "artifact.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ingestion.incremental_planner",
                    "--simulation-input",
                    str(fixture_path),
                    "--output",
                    str(output_path),
                ],
                check=True,
            )
            artifact = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["qdrant_mutations"], 0)
        self.assertEqual(artifact["validation"]["status"], "planned")
        self.assertEqual(artifact["replacement_point_count"], 1)


if __name__ == "__main__":
    unittest.main()
