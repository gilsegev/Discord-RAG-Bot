"""Isolated Phase 9C.6 admission, evidence, and completion-lock checks."""

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
    "13-phase9c6-catchup-migration.sql",
)]


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    schema = f"phase9c6_test_{uuid.uuid4().hex[:12]}"
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
            VALUES ('test_collection',10,10,20) ON CONFLICT DO NOTHING;
            INSERT INTO rag_discord_messages(message_id,capture_sequence,guild_id,channel_id,channel_name,parent_channel_id,parent_channel_name,author_id_hash,author_display_name,content,message_created_at,message_type,normalizer_version)
            VALUES ('1001',1,'g','c','general','c','general','h','a','one',now(),'default','v1'),
                   ('1002',2,'g','c','general','c','general','h','a','two',now(),'default','v1'),
                   ('1003',3,'g','c','general','c','general','h','a','three',now(),'default','v1');
            INSERT INTO rag_pending_chunk_work(source_message_id,capture_sequence,work_kind,parent_channel_id)
            VALUES ('1001',1,'recent_window','c'),('1002',2,'recent_window','c'),('1003',3,'recent_window','c');
        """)
        try:
            db.execute("SELECT * FROM rag_prepare_phase9c6_catchup_attempt('bad','test_collection',3,false,'')")
            raise AssertionError("unaccepted gap should fail")
        except psycopg.Error:
            pass
        stale = db.execute("SELECT * FROM rag_prepare_phase9c6_catchup_attempt('stale','test_collection',2,true,'accepted by owner')").fetchone()
        assert stale[1] == "blocked" and "cutoff_is_not_latest_capture" in stale[6]
        admitted = db.execute("SELECT * FROM rag_prepare_phase9c6_catchup_attempt('catchup','test_collection',3,true,'accepted by owner')").fetchone()
        assert admitted[1:4] == ("planning", 3, 3) and admitted[6] == []
        db.execute("""
            INSERT INTO rag_chunk_replacement_plans(plan_id,collection_name,batch_cutoff_sequence,chunker_version,embedding_version,status,plan_digest,old_point_count,replacement_point_count,pending_message_count,evidence,source_corpus_version_id,source_manifest_digest,estimated_seconds)
            VALUES ('plan-a','test_collection',3,'v10','embed-v1','shadow_validated','digest-a',0,1,3,'{}','corpus-a','manifest-a',2);
        """)
        attached = db.execute("SELECT * FROM rag_attach_incremental_schedule_plan('catchup','plan-a')").fetchone()
        assert attached[1] == "ready"
        db.execute("SELECT rag_finish_incremental_schedule_attempt('catchup','dispatched',NULL,false,'{}')")
        db.execute("SELECT * FROM rag_create_incremental_run('run-a','plan-a','test_collection','{}')")
        db.execute("UPDATE rag_pending_chunk_work SET status='completed',completed_at=now() WHERE capture_sequence<=3")
        db.execute("""
            UPDATE rag_incremental_runs SET run_state='completed',processed_message_count=3,
              corpus_version_after='corpus-a',structural_verification_result='passed',
              regression_result='passed',snapshot_uri='postgres://affected;qdrant://test/full-snapshot',
              completed_at=now() WHERE incremental_run_id='run-a'
        """)
        completed = db.execute("SELECT * FROM rag_complete_phase9c6_catchup('catchup','run-a','{\"targeted_retrieval\":\"pending\"}')").fetchone()
        assert completed[1:] == ("completed", "run-a", True, False, "serving", 0)
        config = db.execute("SELECT catchup_completed,schedule_enabled,config_metadata FROM rag_incremental_schedule_config WHERE collection_name='test_collection'").fetchone()
        assert config[0] is True and config[1] is False
        assert config[2]["accepted_history_gap"] is True
        print("phase9c6 postgres checks passed")
    finally:
        db.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
        db.close()


if __name__ == "__main__":
    main()
