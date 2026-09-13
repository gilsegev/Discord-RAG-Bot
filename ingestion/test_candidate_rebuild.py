import unittest
from datetime import datetime, timezone

from ingestion.candidate_build_cli import qdrant_url_from_env
from ingestion.candidate_rebuild import CandidateRebuildError, _embedding_endpoint, admit_frozen_cutoff, plan_candidate, union_records


def message(message_id, channel_id="10", content="hello", parent_id=None):
    return {"id": str(message_id), "channel_id": channel_id, "channel": "general",
            "thread_name": None, "parent_id": parent_id, "author": "a",
            "content": content, "timestamp": f"2026-01-01T00:0{message_id}:00+00:00"}


class CandidateRebuildTests(unittest.TestCase):
    def test_rejects_caller_future_cutoff(self):
        class Result:
            def fetchone(self): return (7, datetime(2026,1,1,tzinfo=timezone.utc))
        class Connection:
            def execute(self, _): return Result()
        with self.assertRaisesRegex(CandidateRebuildError, "requested cutoff 99"):
            admit_frozen_cutoff(Connection(), 99)

    def test_union_deduplicates_identical_export_and_capture(self):
        self.assertEqual(["1"], [r["id"] for r in union_records([message(1)], [message(1)])])

    def test_union_rejects_conflicting_duplicate_id(self):
        with self.assertRaisesRegex(CandidateRebuildError, "conflicting"):
            union_records([message(1)], [message(1, content="changed")])

    def test_union_normalizes_export_and_capture_shapes(self):
        capture={"message_id":"1","channel_id":10,"parent_channel_name":"GENERAL","thread_id":None,
                 "thread_name":None,"parent_message_id":None,"author_display_name":"a","content":" hello ",
                 "message_created_at":datetime(2026,1,1,0,1,tzinfo=timezone.utc),"has_attachments":False,"capture_sequence":1}
        self.assertEqual(1,len(union_records([message(1)],[capture])))

    def test_plan_fails_when_a_source_message_is_not_chunk_covered(self):
        with self.assertRaisesRegex(CandidateRebuildError, "uncovered=1"):
            plan_candidate([message(1)], [], candidate_collection="candidate", frozen_capture_sequence=0)

    def test_plan_audits_same_channel_ownership(self):
        plan = plan_candidate([message(1), message(2)], [], candidate_collection="candidate", frozen_capture_sequence=7)
        self.assertTrue(plan["structural_audit"]["passed"])
        self.assertEqual(2, plan["source_message_count"])

    def test_plan_accepts_intentional_overlap_between_chunks(self):
        records = [message(i) for i in range(1, 6)]
        for row, minute in zip(records, (0, 1, 20, 21, 40)):
            row["timestamp"] = f"2026-01-01T00:{minute:02d}:00+00:00"
        plan = plan_candidate(records, [], candidate_collection="candidate", frozen_capture_sequence=0)
        occurrences = [mid for row in plan["rows"] for mid in row["message_ids"]]
        self.assertGreater(len(occurrences), len(set(occurrences)))
        self.assertTrue(plan["structural_audit"]["passed"])

    def test_full_embedding_endpoint_is_not_duplicated(self):
        self.assertEqual("http://embedder:8000/embed", _embedding_endpoint("http://embedder:8000/embed"))
        self.assertEqual("http://embedder:8000/embed", _embedding_endpoint("http://embedder:8000"))

    def test_qdrant_base_url_env_takes_precedence(self):
        self.assertEqual("http://production-qdrant", qdrant_url_from_env({
            "QDRANT_BASE_URL": "http://production-qdrant", "QDRANT_URL": "http://legacy-qdrant"
        }))

    def test_export_after_frozen_boundary_is_rejected(self):
        with self.assertRaisesRegex(CandidateRebuildError,"export rows exceed"):
            plan_candidate([message(2)],[],candidate_collection="candidate",frozen_capture_sequence=1,
                           frozen_at=datetime(2026,1,1,0,1,tzinfo=timezone.utc))

    def test_content_change_changes_candidate_identity(self):
        first=plan_candidate([message(1),message(2)],[],candidate_collection="candidate",frozen_capture_sequence=0)
        second=plan_candidate([message(1,content="changed"),message(2)],[],candidate_collection="candidate",frozen_capture_sequence=0)
        self.assertNotEqual(first["candidate_id"],second["candidate_id"])


if __name__ == "__main__":
    unittest.main()
