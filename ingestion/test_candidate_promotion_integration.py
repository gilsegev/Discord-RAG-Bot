import unittest
from pathlib import Path

from ingestion.incremental_executor import ExecutionError, assert_active_pointer


class Result:
    def __init__(self, row): self.row = row
    def fetchone(self): return self.row


class Connection:
    def __init__(self, row): self.row = row
    def execute(self, *_): return Result(self.row)


class CandidatePromotionIntegrationTests(unittest.TestCase):
    def test_post_cutoff_plan_targets_promoted_candidate(self):
        state = {"collection_name": "rag_active", "target_collection_name": "candidate-v2", "source_corpus_version_id": "corpus-v2", "source_manifest_digest": "digest-v2"}
        evidence = {"source_logical_name": "rag_active", "source_active_revision": 9}
        assert_active_pointer(Connection(("candidate-v2", "corpus-v2", "digest-v2", 9, "serving")), state, evidence)

    def test_pre_promotion_plan_is_rejected_after_swap(self):
        state = {"collection_name": "primary-v1", "source_corpus_version_id": "corpus-v1", "source_manifest_digest": "digest-v1"}
        evidence = {"source_logical_name": "rag_active", "source_active_revision": 8}
        with self.assertRaisesRegex(ExecutionError, "pointer changed"):
            assert_active_pointer(Connection(("candidate-v2", "corpus-v2", "digest-v2", 9, "serving")), state, evidence)

    def test_sql_handoff_archives_and_restores_exact_manifest_versions(self):
        sql = Path("deploy/phase0/sql/15-candidate-rebuild-promotion-migration.sql").read_text(encoding="utf-8")
        self.assertIn("rag_corpus_manifest_versions", sql)
        self.assertIn("WHERE corpus_version_id=p.previous_corpus_version_id", sql)
        self.assertIn("WHERE m.candidate_id=p.candidate_id", sql)
        self.assertIn("UPDATE rag_corpus_versions SET status='healthy'", sql)


if __name__ == "__main__":
    unittest.main()
