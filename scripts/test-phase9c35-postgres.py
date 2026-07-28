"""Destructive-in-an-isolated-schema integration test for Phase 9C.3.5.

Usage:
    DATABASE_URL=postgresql://... python scripts/test-phase9c35-postgres.py

The script creates a uniquely named schema, applies the prerequisite and
Phase 9C.3.5 migrations twice, exercises lifecycle/lease behavior, and drops
only that schema on exit.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
import uuid

import psycopg


ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = [
    ROOT / "deploy/phase0/sql/08-phase9c2-chunk-manifest-migration.sql",
    ROOT / "deploy/phase0/sql/09-phase9c3-shadow-plans-migration.sql",
    ROOT / "deploy/phase0/sql/10-phase9c35-run-state-observability-migration.sql",
]


def expect_database_error(connection: psycopg.Connection, sql: str, params=()) -> None:
    try:
        connection.execute(sql, params)
    except psycopg.Error:
        return
    raise AssertionError(f"expected database error: {sql}")


def use_schema(connection: psycopg.Connection, schema: str) -> None:
    connection.execute(
        psycopg.sql.SQL("SET search_path TO {}, public").format(
            psycopg.sql.Identifier(schema)
        )
    )


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    schema = f"phase9c35_test_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(database_url, autocommit=True)
    try:
        # Keep the shared extension outside the disposable test schema.
        admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        admin.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(
                psycopg.sql.Identifier(schema)
            )
        )
        use_schema(admin, schema)

        for migration in MIGRATIONS:
            admin.execute(migration.read_text(encoding="utf-8"))
        # The new migration must preserve data/state and be safe to rerun.
        admin.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))

        admin.execute(
            """
            INSERT INTO rag_ingestion_runs (
                run_id, run_kind, status, collection_name, chunker_version,
                embedding_version, point_count, manifest_digest
            ) VALUES (
                'baseline-test', 'baseline_seed', 'completed',
                'test_collection', 'v10', 'embed-v1', 2, 'manifest-current'
            );
            INSERT INTO rag_corpus_versions (
                corpus_version_id, ingestion_run_id, collection_name,
                manifest_digest, point_count, status, activated_at
            ) VALUES (
                'corpus-current', 'baseline-test', 'test_collection',
                'manifest-current', 2, 'healthy', now()
            );
            INSERT INTO rag_chunk_replacement_plans (
                plan_id, collection_name, batch_cutoff_sequence,
                chunker_version, embedding_version, status, plan_digest,
                old_point_count, replacement_point_count,
                pending_message_count, evidence, source_corpus_version_id,
                source_manifest_digest
            ) VALUES
                ('plan-a', 'test_collection', 10, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-a', 2, 2, 2, '{}',
                 'corpus-current', 'manifest-current'),
                ('plan-b', 'test_collection', 11, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-b', 1, 1, 1, '{}',
                 'corpus-current', 'manifest-current'),
                ('plan-c', 'test_collection', 12, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-c', 1, 1, 1, '{}',
                 'corpus-current', 'manifest-current'),
                ('plan-unvalidated', 'test_collection', 13, 'v10', 'embed-v1',
                 'planned', 'digest-d', 1, 1, 1, '{}', NULL, NULL),
                ('plan-stale', 'test_collection', 14, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-e', 1, 1, 1, '{}',
                 'corpus-current', 'wrong-manifest')
            """
        )

        expect_database_error(
            admin,
            "SELECT * FROM rag_create_incremental_run("
            "'run-unvalidated','plan-unvalidated','test_collection','create')",
        )
        expect_database_error(
            admin,
            "SELECT * FROM rag_create_incremental_run("
            "'run-stale','plan-stale','test_collection','create')",
        )

        created = admin.execute(
            """
            SELECT * FROM rag_create_incremental_run(
                'run-a', 'plan-a', 'test_collection', 'run-a:create',
                'n8n-create-a', '{"simulation": true}'
            )
            """
        ).fetchone()
        assert created[1:] == ("created", "serving", 0, created[4], True)

        duplicate = admin.execute(
            """
            SELECT * FROM rag_create_incremental_run(
                'run-a', 'plan-a', 'test_collection', 'run-a:create',
                'n8n-create-a', '{"simulation": true}'
            )
            """
        ).fetchone()
        assert duplicate[4] == created[4]
        assert duplicate[5] is False
        assert admin.execute(
            "SELECT count(*) FROM rag_incremental_run_events"
        ).fetchone()[0] == 1

        expect_database_error(
            admin,
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'invalid-edge', 'bad', 'created', 'validating',
                'serving', 'validating', 0
            )
            """,
        )
        assert admin.execute(
            "SELECT run_state FROM rag_incremental_runs WHERE incremental_run_id='run-a'"
        ).fetchone()[0] == "created"
        expect_database_error(
            admin,
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'invalid-pair', 'bad', 'created', 'failed',
                'serving', 'draining', 0
            )
            """,
        )

        lease = admin.execute(
            """
            SELECT * FROM rag_acquire_execution_lease(
                'test_collection', 'query-1', 'transaction-1', 'rag-core',
                interval '2 minutes', '{"test": true}'
            )
            """
        ).fetchone()
        duplicate_lease = admin.execute(
            """
            SELECT * FROM rag_acquire_execution_lease(
                'test_collection', 'query-1', 'transaction-1', 'rag-core',
                interval '2 minutes', '{"test": true}'
            )
            """
        ).fetchone()
        assert duplicate_lease[0] == lease[0]
        assert duplicate_lease[4] is False
        assert admin.execute(
            "SELECT rag_count_live_execution_leases('test_collection')"
        ).fetchone()[0] == 1
        admin.execute(
            "SELECT * FROM rag_heartbeat_execution_lease(%s, interval '3 minutes')",
            (lease[0],),
        )
        admin.execute(
            "SELECT * FROM rag_heartbeat_execution_lease(%s, interval '15 minutes')",
            (lease[0],),
        )
        capped_expiry = admin.execute(
            "SELECT expires_at, hard_expires_at FROM rag_active_execution_leases "
            "WHERE lease_id=%s",
            (lease[0],),
        ).fetchone()
        assert capped_expiry[0] == capped_expiry[1]

        draining = admin.execute(
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'run-a:draining', 'incremental.draining_started',
                'created', 'draining', 'serving', 'draining', 0,
                'n8n-drain-a', '{}'
            )
            """
        ).fetchone()
        assert draining[1:4] == ("draining", "draining", 1)
        duplicate_draining = admin.execute(
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'run-a:draining', 'incremental.draining_started',
                'created', 'draining', 'serving', 'draining', 0,
                'n8n-drain-a', '{}'
            )
            """
        ).fetchone()
        assert duplicate_draining[4] == draining[4]
        assert duplicate_draining[5] is False

        expect_database_error(
            admin,
            """
            SELECT * FROM rag_acquire_execution_lease(
                'test_collection', 'query-after-drain'
            )
            """,
        )
        expect_database_error(
            admin,
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'run-a:maintenance-too-early',
                'incremental.maintenance_entered',
                'draining', 'maintenance', 'draining', 'maintenance', 1
            )
            """,
        )
        admin.execute("SELECT * FROM rag_release_execution_lease(%s)", (lease[0],))
        assert admin.execute(
            "SELECT rag_count_live_execution_leases('test_collection')"
        ).fetchone()[0] == 0

        admin.execute(
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'run-a:maintenance', 'incremental.maintenance_entered',
                'draining', 'maintenance', 'draining', 'maintenance', 1
            )
            """
        )
        admin.execute(
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'run-a:validating', 'incremental.validation_started',
                'maintenance', 'validating', 'maintenance', 'validating', 2
            )
            """
        )
        admin.execute(
            """
            SELECT * FROM rag_transition_incremental_run(
                'run-a', 'run-a:complete', 'incremental.serving_restored',
                'validating', 'completed', 'validating', 'serving', 3
            )
            """
        )

        expect_database_error(
            admin,
            "UPDATE rag_incremental_run_events SET event_name='tampered' WHERE event_id=%s",
            (created[4],),
        )
        expect_database_error(
            admin,
            "DELETE FROM rag_incremental_run_events WHERE event_id=%s",
            (created[4],),
        )

        # Expired leases are excluded and cannot be revived.
        expiring = admin.execute(
            """
            SELECT * FROM rag_acquire_execution_lease(
                'test_collection', 'query-expiring', NULL, NULL,
                interval '50 milliseconds'
            )
            """
        ).fetchone()
        time.sleep(0.08)
        assert admin.execute(
            "SELECT rag_count_live_execution_leases('test_collection')"
        ).fetchone()[0] == 0
        expect_database_error(
            admin,
            "SELECT * FROM rag_heartbeat_execution_lease(%s)",
            (expiring[0],),
        )

        # Two coordinators using the same runtime revision race. The row lock
        # and revision CAS permit exactly one serving -> draining transition.
        for run_id, plan_id in (("run-b", "plan-b"), ("run-c", "plan-c")):
            admin.execute(
                "SELECT * FROM rag_create_incremental_run(%s,%s,'test_collection',%s)",
                (run_id, plan_id, f"{run_id}:create"),
            )

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def compete(run_id: str) -> None:
            connection = psycopg.connect(database_url, autocommit=True)
            try:
                use_schema(connection, schema)
                barrier.wait()
                connection.execute(
                    """
                    SELECT * FROM rag_transition_incremental_run(
                        %s, %s, 'incremental.draining_started',
                        'created', 'draining', 'serving', 'draining', 4
                    )
                    """,
                    (run_id, f"{run_id}:draining"),
                )
                outcome = "success"
            except psycopg.Error:
                outcome = "rejected"
            finally:
                connection.close()
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=compete, args=("run-b",)),
            threading.Thread(target=compete, args=("run-c",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes) == ["rejected", "success"], outcomes

        print("phase9c35 postgres integration checks passed")
    finally:
        admin.execute("RESET search_path")
        admin.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(schema)
            )
        )
        admin.close()


if __name__ == "__main__":
    main()
