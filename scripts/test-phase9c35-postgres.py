"""Isolated-schema integration checks for the Phase 9C.3.5 Postgres API."""

from __future__ import annotations

import os
import pathlib
import time
import uuid

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = [
    ROOT / "deploy/phase0/sql/08-phase9c2-chunk-manifest-migration.sql",
    ROOT / "deploy/phase0/sql/09-phase9c3-shadow-plans-migration.sql",
    ROOT / "deploy/phase0/sql/10-phase9c35-run-state-observability-migration.sql",
]


def use_schema(connection: psycopg.Connection, schema: str) -> None:
    connection.execute(
        psycopg.sql.SQL("SET search_path TO {}, public").format(
            psycopg.sql.Identifier(schema)
        )
    )


def expect_error(connection: psycopg.Connection, query: str, params=()) -> None:
    try:
        connection.execute(query, params)
    except psycopg.Error:
        return
    raise AssertionError(f"expected database error: {query}")


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    schema = f"phase9c35_test_{uuid.uuid4().hex[:12]}"
    db = psycopg.connect(database_url, autocommit=True)
    try:
        db.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(
                psycopg.sql.Identifier(schema)
            )
        )
        use_schema(db, schema)
        for migration in MIGRATIONS:
            db.execute(migration.read_text(encoding="utf-8"))
        db.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))
        assert db.execute(
            "SELECT to_regclass('rag_incremental_run_events')"
        ).fetchone()[0] is None

        db.execute(
            """
            INSERT INTO rag_ingestion_runs (
                run_id, run_kind, status, collection_name, chunker_version,
                embedding_version, point_count, manifest_digest
            ) VALUES (
                'baseline', 'baseline_seed', 'completed', 'test_collection',
                'v10', 'embed-v1', 0, 'manifest-current'
            );
            INSERT INTO rag_corpus_versions (
                corpus_version_id, ingestion_run_id, collection_name,
                manifest_digest, point_count, status
            ) VALUES (
                'corpus-current', 'baseline', 'test_collection',
                'manifest-current', 0, 'healthy'
            );
            INSERT INTO rag_chunk_replacement_plans (
                plan_id, collection_name, batch_cutoff_sequence,
                chunker_version, embedding_version, status, plan_digest,
                old_point_count, replacement_point_count,
                pending_message_count, evidence, source_corpus_version_id,
                source_manifest_digest
            ) VALUES
                ('plan-a', 'test_collection', 10, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-a', 0, 1, 1, '{}',
                 'corpus-current', 'manifest-current'),
                ('plan-b', 'test_collection', 11, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-b', 0, 1, 1, '{}',
                 'corpus-current', 'manifest-current'),
                ('plan-stale', 'test_collection', 12, 'v10', 'embed-v1',
                 'shadow_validated', 'digest-c', 0, 1, 1, '{}',
                 'corpus-current', 'wrong')
            """
        )
        expect_error(
            db,
            "SELECT * FROM rag_create_incremental_run("
            "'stale','plan-stale','test_collection')",
        )

        created = db.execute(
            "SELECT * FROM rag_create_incremental_run("
            "'run-a','plan-a','test_collection','{\"simulation\":true}')"
        ).fetchone()
        assert created == ("run-a", "created", "serving", 0)
        assert db.execute(
            "SELECT * FROM rag_create_incremental_run("
            "'run-a','plan-a','test_collection','{\"simulation\":true}')"
        ).fetchone() == created
        updated = db.execute(
            "SELECT * FROM rag_update_incremental_run("
            "'run-a','{\"processed_message_count\":1,\"verified\":true}')"
        ).fetchone()
        assert updated == created
        assert db.execute(
            "SELECT processed_message_count FROM rag_incremental_runs "
            "WHERE incremental_run_id='run-a'"
        ).fetchone()[0] == 1

        lease = db.execute(
            "SELECT * FROM rag_acquire_execution_lease("
            "'test_collection','query-1','tx-1','rag-core',interval '2 minutes')"
        ).fetchone()
        assert lease[4] is True
        assert db.execute(
            "SELECT rag_count_live_execution_leases('test_collection')"
        ).fetchone()[0] == 1
        draining = db.execute(
            "SELECT * FROM rag_begin_incremental_drain('run-a',0)"
        ).fetchone()
        assert draining == ("run-a", "draining", "draining", 1)
        assert db.execute(
            "SELECT * FROM rag_begin_incremental_drain('run-a',0)"
        ).fetchone() == draining
        expect_error(
            db,
            "SELECT * FROM rag_acquire_execution_lease("
            "'test_collection','query-denied')",
        )
        expect_error(
            db,
            "SELECT * FROM rag_enter_incremental_maintenance('run-a',1)",
        )
        db.execute("SELECT * FROM rag_release_execution_lease(%s)", (lease[0],))

        entered = db.execute(
            "SELECT * FROM rag_enter_incremental_maintenance('run-a',1)"
        ).fetchone()
        assert entered == ("run-a", "maintenance", "maintenance", 2)
        assert db.execute(
            "SELECT * FROM rag_enter_incremental_maintenance('run-a',1)"
        ).fetchone() == entered
        exited = db.execute(
            "SELECT * FROM rag_exit_incremental_maintenance("
            "'run-a','completed','{\"structural_validation\":true}')"
        ).fetchone()
        assert exited == ("run-a", "completed", "serving", 3)
        assert db.execute(
            "SELECT * FROM rag_exit_incremental_maintenance("
            "'run-a','completed','{\"structural_validation\":true}')"
        ).fetchone() == exited

        db.execute(
            "SELECT * FROM rag_create_incremental_run("
            "'run-b','plan-b','test_collection','{\"simulation\":true}')"
        )
        failed = db.execute(
            "SELECT * FROM rag_fail_incremental_run("
            "'run-b','simulation','expected failure','{}')"
        ).fetchone()
        assert failed == ("run-b", "failed", "serving", 3)

        expiring = db.execute(
            "SELECT * FROM rag_acquire_execution_lease("
            "'test_collection','query-expiring',NULL,NULL,"
            "interval '50 milliseconds')"
        ).fetchone()
        time.sleep(0.08)
        assert db.execute(
            "SELECT rag_count_live_execution_leases('test_collection')"
        ).fetchone()[0] == 0
        expect_error(
            db,
            "SELECT * FROM rag_heartbeat_execution_lease(%s)",
            (expiring[0],),
        )
        print("phase9c35 postgres integration checks passed")
    finally:
        db.execute("RESET search_path")
        db.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(schema)
            )
        )
        db.close()


if __name__ == "__main__":
    main()
