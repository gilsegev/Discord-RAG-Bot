"""Isolated-schema integration checks for Phase 9C.4 lifecycle gates."""

from __future__ import annotations

import os
import pathlib
import uuid

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = [
    ROOT / "deploy/phase0/sql/07-phase9c1-incremental-capture-migration.sql",
    ROOT / "deploy/phase0/sql/08-phase9c2-chunk-manifest-migration.sql",
    ROOT / "deploy/phase0/sql/09-phase9c3-shadow-plans-migration.sql",
    ROOT / "deploy/phase0/sql/10-phase9c35-run-state-observability-migration.sql",
    ROOT / "deploy/phase0/sql/11-phase9c4-production-replacement-migration.sql",
]


def expect_error(db, query, params=()):
    try:
        db.execute(query, params)
    except psycopg.Error:
        return
    raise AssertionError(f"expected database error: {query}")


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    schema = f"phase9c4_test_{uuid.uuid4().hex[:12]}"
    db = psycopg.connect(database_url, autocommit=True)
    try:
        db.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        db.execute(psycopg.sql.SQL("SET search_path TO {}, public").format(psycopg.sql.Identifier(schema)))
        for migration in MIGRATIONS[:-1]:
            db.execute(migration.read_text(encoding="utf-8"))
        db.execute(
            """CREATE TABLE rag_regression_runs (
                run_id TEXT PRIMARY KEY,status TEXT,case_count INTEGER,
                pass_count INTEGER,fail_count INTEGER,review_count INTEGER
            )"""
        )
        db.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))
        db.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))

        db.execute(
            """
            INSERT INTO rag_ingestion_runs
                (run_id,run_kind,status,collection_name,chunker_version,
                 embedding_version,point_count,manifest_digest)
            VALUES ('baseline','baseline_seed','completed','test_collection','v10','embed-v1',0,'manifest-current');
            INSERT INTO rag_corpus_versions
                (corpus_version_id,ingestion_run_id,collection_name,manifest_digest,point_count,status)
            VALUES ('corpus-current','baseline','test_collection','manifest-current',0,'healthy');
            INSERT INTO rag_discord_messages
                (message_id,capture_sequence,guild_id,channel_id,channel_name,
                 parent_channel_id,parent_channel_name,author_id_hash,author_display_name,
                 content,message_created_at,message_type,normalizer_version)
            VALUES ('1001',1,'g','c','general','c','general','h','a','one',now(),'default','v1'),
                   ('1002',2,'g','c','general','c','general','h','a','two',now(),'default','v1');
            INSERT INTO rag_pending_chunk_work
                (source_message_id,capture_sequence,work_kind,parent_channel_id)
            VALUES ('1001',1,'recent_window','c'),('1002',2,'recent_window','c');
            INSERT INTO rag_chunk_replacement_plans
                (plan_id,collection_name,batch_cutoff_sequence,chunker_version,
                 embedding_version,status,plan_digest,old_point_count,
                 replacement_point_count,pending_message_count,evidence,
                 source_corpus_version_id,source_manifest_digest)
            VALUES ('plan-a','test_collection',2,'v10','embed-v1','shadow_validated',
                    'digest-a',0,1,2,'{"groups":[]}','corpus-current','manifest-current'),
                   ('plan-b','test_collection',2,'v10','embed-v1','shadow_validated',
                    'digest-b',0,1,2,'{"groups":[]}','corpus-current','manifest-current');
            INSERT INTO rag_chunk_replacement_plan_groups
                (plan_id,group_key,work_kind,channel_id,source_message_ids,
                 old_point_ids,replacement_point_ids,status,evidence)
            VALUES ('plan-a','window:c:-:x','recent_window','c',ARRAY['1001','1002'],
                    ARRAY[]::text[],ARRAY['2001'],'ready','{}'),
                   ('plan-b','window:c:-:x','recent_window','c',ARRAY['1001','1002'],
                    ARRAY[]::text[],ARRAY['2002'],'ready','{}');
            INSERT INTO rag_regression_runs VALUES
                ('reg-before','completed',48,43,1,4),
                ('reg-after','completed',48,43,1,4);
            """
        )
        assert db.execute(
            "SELECT * FROM rag_create_incremental_run('run-a','plan-a','test_collection','{}')"
        ).fetchone() == ("run-a", "created", "serving", 0)
        db.execute("SELECT * FROM rag_record_incremental_regression('run-a','reg-before','passed',true)")

        lease = db.execute(
            "SELECT * FROM rag_acquire_execution_lease('test_collection','query-1')"
        ).fetchone()
        assert lease[5:] == (True, None, False)
        assert db.execute("SELECT * FROM rag_begin_incremental_drain('run-a',0)").fetchone()[1:] == (
            "draining", "draining", 1
        )
        denied = db.execute(
            "SELECT * FROM rag_acquire_execution_lease('test_collection','query-denied')"
        ).fetchone()
        assert denied[5:] == (False, "maintenance_in_progress", False)
        db.execute("SELECT * FROM rag_release_execution_lease(%s)", (lease[0],))
        db.execute("SELECT * FROM rag_enter_incremental_maintenance('run-a',1)")
        validation = db.execute(
            "SELECT * FROM rag_acquire_execution_lease('test_collection','validation',NULL,NULL,interval '2 minutes','{}','run-a')"
        ).fetchone()
        assert validation[5:] == (True, None, True)
        db.execute("SELECT * FROM rag_release_execution_lease(%s)", (validation[0],))
        claimed = db.execute("SELECT * FROM rag_mark_incremental_replacing('run-a',2)").fetchone()
        assert claimed[1:] == ("replacing", "maintenance", 2, 2)
        db.execute(
            "UPDATE rag_incremental_runs SET run_state='validating',structural_verification_result='passed' WHERE incremental_run_id='run-a'"
        )
        db.execute("SELECT * FROM rag_record_incremental_regression('run-a','reg-after','passed',false)")
        exited = db.execute("SELECT * FROM rag_exit_incremental_maintenance('run-a','completed','{}')").fetchone()
        assert exited == ("run-a", "completed", "serving", 3)

        db.execute("UPDATE rag_pending_chunk_work SET status='pending',claimed_at=NULL,completed_at=NULL")
        db.execute("SELECT * FROM rag_create_incremental_run('run-b','plan-b','test_collection','{}')")
        db.execute("SELECT * FROM rag_begin_incremental_drain('run-b',3)")
        db.execute("SELECT * FROM rag_enter_incremental_maintenance('run-b',4)")
        db.execute("SELECT * FROM rag_mark_incremental_replacing('run-b',5)")
        expect_error(db, "SELECT * FROM rag_exit_incremental_maintenance('run-b','failed','{}')")
        db.execute("UPDATE rag_incremental_runs SET run_state='maintenance',rollback_status='completed' WHERE incremental_run_id='run-b'")
        failed = db.execute("SELECT * FROM rag_exit_incremental_maintenance('run-b','failed','{}')").fetchone()
        assert failed == ("run-b", "failed", "serving", 6)
        print("phase9c4 postgres integration checks passed")
    finally:
        db.execute("RESET search_path")
        db.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))
        db.close()


if __name__ == "__main__":
    main()
