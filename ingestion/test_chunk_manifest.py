import copy
import unittest

from ingestion.chunk_manifest import (
    OwnershipError,
    apply_plan,
    create_plan,
    verify_plan,
)
from ingestion.run import _stable_id


def record(message_id, parent_id=None, channel_id="10", thread_name=None):
    return {
        "id": str(message_id),
        "parent_id": str(parent_id) if parent_id else None,
        "channel_id": channel_id,
        "thread_name": thread_name,
    }


def point(message_ids, split_index=0, channel_id="10", **overrides):
    payload = {
        "message_ids": [str(value) for value in message_ids],
        "first_message_id": str(message_ids[0]),
        "split_index": split_index,
        "channel_id": channel_id,
        "thread_name": None,
    }
    payload.update(overrides)
    point_id = _stable_id(
        {"message_ids": payload["message_ids"], "split_index": split_index}
    )
    return str(point_id), payload


class ChunkManifestTests(unittest.TestCase):
    def test_plan_is_deterministic_and_order_independent(self):
        records = [record(1), record(2), record(3)]
        points = [point([1, 2]), point([2, 3])]
        first = create_plan(points, records, "c", "chunker", "embed")
        second = create_plan(reversed(points), reversed(records), "c", "chunker", "embed")
        self.assertEqual(first, second)
        self.assertEqual(first["point_count"], 2)

    def test_reply_chain_gets_one_root_group(self):
        records = [record(1), record(2, 1), record(3, 2)]
        plan = create_plan([point([1, 2, 3])], records, "c", "v10", "embed")
        row = plan["rows"][0]
        self.assertEqual(row["root_message_id"], "1")
        self.assertTrue(row["logical_group_id"].endswith(":1"))

    def test_split_reply_piece_inherits_omitted_root(self):
        records = [record(1), record(2, 1), record(3, 2)]
        plan = create_plan([point([2, 3])], records, "c", "v10", "embed")
        self.assertEqual(plan["rows"][0]["root_message_id"], "1")
        self.assertTrue(plan["rows"][0]["logical_group_id"].endswith(":1"))

    def test_missing_export_message_uses_payload_ownership(self):
        plan = create_plan([point([1, 2])], [record(1)], "c", "v10", "embed")
        self.assertIsNone(plan["rows"][0]["root_message_id"])

    def test_missing_payload_fails_closed(self):
        point_id, payload = point([1])
        del payload["channel_id"]
        with self.assertRaisesRegex(OwnershipError, "required payload"):
            create_plan([(point_id, payload)], [record(1)], "c", "v10", "embed")

    def test_wrong_point_id_fails_closed(self):
        _, payload = point([1])
        with self.assertRaisesRegex(OwnershipError, "stable ID"):
            create_plan([("123", payload)], [record(1)], "c", "v10", "embed")

    def test_verify_detects_payload_change(self):
        points = [point([1])]
        plan = create_plan(points, None, "c", "v10", "embed")
        changed_id, changed_payload = points[0]
        changed_payload = {**changed_payload, "channel_id": "changed"}
        with self.assertRaisesRegex(OwnershipError, "payload_mismatch"):
            verify_plan(plan, [(changed_id, changed_payload)])

    def test_verify_detects_saved_plan_corruption(self):
        points = [point([1])]
        plan = create_plan(points, None, "c", "v10", "embed")
        corrupted = copy.deepcopy(plan)
        corrupted["rows"][0]["logical_group_id"] = "tampered"
        with self.assertRaisesRegex(OwnershipError, "manifest_digest"):
            verify_plan(corrupted, points)

    def test_verify_detects_saved_point_count_corruption(self):
        points = [point([1])]
        plan = create_plan(points, None, "c", "v10", "embed")
        plan["point_count"] = 99
        with self.assertRaisesRegex(OwnershipError, "point_count"):
            verify_plan(plan, points)

    def test_runtime_versions_change_run_identity(self):
        points = [point([1])]
        first = create_plan(points, None, "c", "v10", "embed-v1")
        repeat = create_plan(points, None, "c", "v10", "embed-v1")
        changed_chunker = create_plan(points, None, "c", "v11", "embed-v1")
        changed_embedding = create_plan(points, None, "c", "v10", "embed-v2")
        changed_collection = create_plan(points, None, "other", "v10", "embed-v1")
        self.assertEqual(first["run_id"], repeat["run_id"])
        self.assertEqual(len({
            first["run_id"],
            changed_chunker["run_id"],
            changed_embedding["run_id"],
            changed_collection["run_id"],
        }), 4)

    def test_apply_reactivates_same_corpus_version(self):
        class Context:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self.value
            def __exit__(self, *_):
                return False

        class Cursor:
            def __init__(self):
                self.statements = []
            def execute(self, sql, params=None):
                self.statements.append(sql)
            def copy(self, sql):
                class Copy:
                    def write_row(self, row):
                        pass
                return Context(Copy())
            def fetchone(self):
                return (1,)

        class Connection:
            def __init__(self):
                self.cursor_value = Cursor()
            def transaction(self):
                return Context(None)
            def cursor(self):
                return Context(self.cursor_value)

        plan = create_plan([point([1])], None, "c", "v10", "embed")
        connection = Connection()
        apply_plan(connection, plan)
        corpus_upsert = next(
            sql for sql in connection.cursor_value.statements
            if "INSERT INTO rag_corpus_versions" in sql
        )
        self.assertIn("status='healthy'", corpus_upsert)
        self.assertIn("superseded_at=NULL", corpus_upsert)

    def test_apply_uses_copy_and_set_based_writes(self):
        class Context:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self.value
            def __exit__(self, *_):
                return False

        class Cursor:
            def __init__(self):
                self.statements = []
                self.copies = []
            def execute(self, sql, params=None):
                self.statements.append(sql)
            def copy(self, sql):
                class Copy:
                    def __init__(self):
                        self.rows = []
                    def write_row(self, row):
                        self.rows.append(row)
                copy = Copy()
                self.copies.append((sql, copy))
                return Context(copy)
            def fetchone(self):
                return (2001,)

        class Connection:
            def __init__(self):
                self.cursor_value = Cursor()
            def transaction(self):
                return Context(None)
            def cursor(self):
                return Context(self.cursor_value)

        points = [point([message_id]) for message_id in range(1, 2002)]
        plan = create_plan(points, None, "c", "v10", "embed")
        connection = Connection()
        apply_plan(connection, plan)
        manifest_copies = [
            copy for sql, copy in connection.cursor_value.copies
            if "COPY rag_chunk_manifest_stage" in sql
        ]
        ownership_copies = [
            copy for sql, copy in connection.cursor_value.copies
            if "COPY rag_chunk_ownership_stage" in sql
        ]
        self.assertEqual(len(manifest_copies[0].rows), 2001)
        self.assertEqual(len(ownership_copies[0].rows), 2001)
        self.assertTrue(any(
            "FROM rag_chunk_manifest_stage" in sql
            for sql in connection.cursor_value.statements
        ))
        self.assertTrue(any(
            "FROM rag_chunk_ownership_stage" in sql
            for sql in connection.cursor_value.statements
        ))

    def test_reply_cycle_fails_closed(self):
        records = [record(1, 2), record(2, 1)]
        with self.assertRaisesRegex(OwnershipError, "reply cycle"):
            create_plan([point([1, 2])], records, "c", "v10", "embed")

    def test_orphan_replies_with_multiple_roots_are_a_window(self):
        records = [record(1), record(2, 1), record(3), record(4, 3)]
        plan = create_plan(
            [point([1, 2, 3, 4])], records, "c", "v10", "embed"
        )
        self.assertIsNone(plan["rows"][0]["root_message_id"])
        self.assertTrue(plan["rows"][0]["logical_group_id"].startswith("point:"))

    def test_historical_cross_channel_context_keeps_payload_owner(self):
        records = [record(1), record(2, channel_id="11")]
        plan = create_plan(
            [point([1, 2], channel_id="10")], records, "c", "v10", "embed"
        )
        self.assertEqual(plan["rows"][0]["channel_id"], "10")
        self.assertTrue(plan["rows"][0]["logical_group_id"].startswith("point:10:"))

    def test_cross_channel_split_reply_does_not_infer_root(self):
        records = [
            record(1, channel_id="11"),
            record(2, 1, channel_id="10"),
            record(3, 2, channel_id="10"),
        ]
        plan = create_plan(
            [point([2, 3], channel_id="10")], records, "c", "v10", "embed"
        )
        self.assertIsNone(plan["rows"][0]["root_message_id"])


if __name__ == "__main__":
    unittest.main()
