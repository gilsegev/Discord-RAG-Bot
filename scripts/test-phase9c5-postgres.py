"""Isolated Phase 9C.5 schedule guards, reports, alerts, and drain recovery."""

from __future__ import annotations

import os
import pathlib
import uuid

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = [ROOT / f"deploy/phase0/sql/{name}" for name in (
    "07-phase9c1-incremental-capture-migration.sql",
    "08-phase9c2-chunk-manifest-migration.sql",
    "09-phase9c3-shadow-plans-migration.sql",
    "10-phase9c35-run-state-observability-migration.sql",
    "11-phase9c4-production-replacement-migration.sql",
    "12-phase9c5-scheduled-readiness-migration.sql",
)]


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    schema = f"phase9c5_test_{uuid.uuid4().hex[:12]}"
    db = psycopg.connect(database_url, autocommit=True)
    try:
        db.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        db.execute(psycopg.sql.SQL("SET search_path TO {}, public").format(psycopg.sql.Identifier(schema)))
        for migration in MIGRATIONS[:4]:
            db.execute(migration.read_text(encoding="utf-8"))
        db.execute("CREATE TABLE rag_regression_runs (run_id UUID PRIMARY KEY,status TEXT,case_count INTEGER,pass_count INTEGER,fail_count INTEGER,review_count INTEGER)")
        for migration in MIGRATIONS[4:]:
            db.execute(migration.read_text(encoding="utf-8"))
            db.execute(migration.read_text(encoding="utf-8"))
        db.execute("""
            INSERT INTO rag_ingestion_runs(run_id,run_kind,status,collection_name,chunker_version,embedding_version,point_count,manifest_digest)
            VALUES ('baseline','baseline_seed','completed','test_collection','v10','embed-v1',0,'manifest-a');
            INSERT INTO rag_corpus_versions(corpus_version_id,ingestion_run_id,collection_name,manifest_digest,point_count,status)
            VALUES ('corpus-a','baseline','test_collection','manifest-a',0,'healthy');
            INSERT INTO rag_runtime_state(collection_name) VALUES ('test_collection') ON CONFLICT DO NOTHING;
            INSERT INTO rag_incremental_schedule_config(collection_name,max_messages_per_run,max_replacement_points,max_estimated_seconds)
            VALUES ('test_collection',2,4,20) ON CONFLICT DO NOTHING;
            INSERT INTO rag_discord_messages(message_id,capture_sequence,guild_id,channel_id,channel_name,parent_channel_id,parent_channel_name,author_id_hash,author_display_name,content,message_created_at,message_type,normalizer_version)
            VALUES ('1001',1,'g','c','general','c','general','h','a','one',now(),'default','v1'),
                   ('1002',2,'g','c','general','c','general','h','a','two',now(),'default','v1'),
                   ('1003',3,'g','c','general','c','general','h','a','three',now(),'default','v1');
            INSERT INTO rag_pending_chunk_work(source_message_id,capture_sequence,work_kind,parent_channel_id)
            VALUES ('1001',1,'recent_window','c'),('1002',2,'recent_window','c'),('1003',3,'recent_window','c');
        """)
        disabled = db.execute("SELECT * FROM rag_prepare_incremental_schedule_attempt('scheduled-a','test_collection','scheduled')").fetchone()
        assert disabled[1] == "disabled" and disabled[-1] is False
        assert "schedule_disabled" in disabled[8] and "phase9c6_catchup_required" in disabled[8]
        blocked = db.execute("SELECT * FROM rag_prepare_incremental_schedule_attempt('dry-a','test_collection','manual_dry_run')").fetchone()
        assert blocked[1] == "blocked" and "phase9c6_catchup_required" in blocked[8]
        db.execute("UPDATE rag_incremental_schedule_config SET catchup_completed=true WHERE collection_name='test_collection'")
        planning = db.execute("SELECT * FROM rag_prepare_incremental_schedule_attempt('dry-b','test_collection','manual_dry_run')").fetchone()
        assert planning[1] == "planning" and planning[4:6] == (2, 2)
        db.execute("""
            INSERT INTO rag_chunk_replacement_plans(plan_id,collection_name,batch_cutoff_sequence,chunker_version,embedding_version,status,plan_digest,old_point_count,replacement_point_count,pending_message_count,evidence,source_corpus_version_id,source_manifest_digest,estimated_seconds)
            VALUES ('plan-a','test_collection',2,'v10','embed-v1','shadow_validated','digest-a',0,1,2,'{}','corpus-a','manifest-a',2);
        """)
        attached = db.execute("SELECT * FROM rag_attach_incremental_schedule_plan('dry-b','plan-a')").fetchone()
        assert attached[1] == "ready" and attached[3] == []
        db.execute("SELECT rag_finish_incremental_schedule_attempt('dry-b','dispatched',NULL,false,'{}')")
        db.execute("SELECT * FROM rag_create_incremental_run('run-a','plan-a','test_collection','{}')")
        overlap = db.execute("SELECT * FROM rag_prepare_incremental_schedule_attempt('dry-c','test_collection','manual_dry_run')").fetchone()
        assert overlap[1] == "blocked" and "overlapping_incremental_run" in overlap[8]
        db.execute("SELECT * FROM rag_begin_incremental_drain('run-a',0)")
        cancelled = db.execute("SELECT * FROM rag_cancel_incremental_drain('run-a','test_timeout')").fetchone()
        assert cancelled[1:] == ("failed", "serving", 2)
        first = db.execute("SELECT (rag_queue_incremental_alert('dry-c','warning','blocked','dry-c:blocked','{}')).alert_id").fetchone()[0]
        second = db.execute("SELECT (rag_queue_incremental_alert('dry-c','warning','blocked','dry-c:blocked','{}')).alert_id").fetchone()[0]
        assert first == second
        assert db.execute("SELECT count(*) FROM rag_incremental_run_reports WHERE attempt_id IN ('dry-b','dry-c')").fetchone()[0] == 2
        print("phase9c5 postgres checks passed")
    finally:
        db.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
        db.close()


if __name__ == "__main__":
    main()
