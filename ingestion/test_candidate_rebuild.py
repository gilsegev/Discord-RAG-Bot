import unittest

from ingestion.candidate_rebuild import CandidateRebuildError, plan_candidate, union_records


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

    def test_plan_fails_when_a_source_message_is_not_chunk_covered(self):
        with self.assertRaisesRegex(CandidateRebuildError, "uncovered=1"):
            plan_candidate([message(1)], [], candidate_collection="candidate", frozen_capture_sequence=0)

    def test_plan_audits_same_channel_ownership(self):
        plan = plan_candidate([message(1), message(2)], [], candidate_collection="candidate", frozen_capture_sequence=7)
        self.assertTrue(plan["structural_audit"]["passed"])
        self.assertEqual(2, plan["source_message_count"])


if __name__ == "__main__":
    unittest.main()
