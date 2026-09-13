import unittest

from datetime import datetime, timezone

from ingestion.candidate_rebuild import CandidateRebuildError, normalize_source_record, plan_candidate, union_records


def message(message_id, channel_id="10", content="hello", parent_id=None):
    return {"id": str(message_id), "channel_id": channel_id, "channel": "general",
            "thread_name": None, "parent_id": parent_id, "author": "a",
            "content": content, "timestamp": f"2026-01-01T00:0{message_id}:00+00:00"}


class CandidateRebuildTests(unittest.TestCase):
    def test_union_deduplicates_identical_export_and_capture(self):
        self.assertEqual(["1"], [r["id"] for r in union_records([message(1)], [message(1)])])

    def test_union_rejects_conflicting_duplicate_id(self):
        with self.assertRaisesRegex(CandidateRebuildError, "conflicting"):
            union_records([message(1)], [message(1, content="changed")])

    def test_union_compares_export_and_capture_after_field_normalization(self):
        export = message(1)
        capture = {"message_id": "1", "channel_id": 10, "parent_channel_name": "GENERAL",
                   "thread_id": None, "thread_name": None, "parent_message_id": None,
                   "author_display_name": "a", "content": " hello ",
                   "message_created_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
                   "has_attachments": False, "capture_sequence": 9, "author_id_hash": "ignored"}
        self.assertEqual(["1"], [row["id"] for row in union_records([export], [capture])])

    def test_normalization_rejects_naive_timestamp(self):
        record = message(1)
        record["timestamp"] = "2026-01-01T00:00:00"
        with self.assertRaisesRegex(CandidateRebuildError, "offset"):
            normalize_source_record(record)

    def test_plan_rejects_capture_after_cutoff(self):
        capture = {**message(1), "capture_sequence": 8}
        with self.assertRaisesRegex(CandidateRebuildError, "exceed frozen cutoff"):
            plan_candidate([message(2)], [capture], candidate_collection="candidate", frozen_capture_sequence=7)

    def test_plan_fails_when_a_source_message_is_not_chunk_covered(self):
        with self.assertRaisesRegex(CandidateRebuildError, "uncovered=1"):
            plan_candidate([message(1)], [], candidate_collection="candidate", frozen_capture_sequence=0)

    def test_plan_audits_same_channel_ownership(self):
        plan = plan_candidate([message(1), message(2)], [], candidate_collection="candidate", frozen_capture_sequence=7)
        self.assertTrue(plan["structural_audit"]["passed"])
        self.assertEqual(2, plan["source_message_count"])
        self.assertTrue(plan["corpus_version_id"].startswith("corpus-"))

    def test_content_change_changes_candidate_identity(self):
        first = plan_candidate([message(1), message(2)], [], candidate_collection="candidate", frozen_capture_sequence=7)
        changed = plan_candidate([message(1, content="changed"), message(2)], [], candidate_collection="candidate", frozen_capture_sequence=7)
        self.assertNotEqual(first["candidate_id"], changed["candidate_id"])
        self.assertNotEqual(first["candidate_payload_digest"], changed["candidate_payload_digest"])

    def test_collection_name_changes_candidate_identity(self):
        first = plan_candidate([message(1), message(2)], [], candidate_collection="candidate-a", frozen_capture_sequence=7)
        second = plan_candidate([message(1), message(2)], [], candidate_collection="candidate-b", frozen_capture_sequence=7)
        self.assertNotEqual(first["candidate_id"], second["candidate_id"])


if __name__ == "__main__":
    unittest.main()
