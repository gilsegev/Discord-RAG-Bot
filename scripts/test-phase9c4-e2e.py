"""Phase 9C.4 apply and rollback integration test with in-memory Qdrant."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from ingestion.incremental_executor import ExecutionError, apply_replacement, rollback_replacement
from ingestion.incremental_planner import (
    create_shadow_plan,
    embed_shadow,
    load_postgres,
    persist_plan,
    render_plan,
)

MIGRATIONS = [
    ROOT / "deploy/phase0/sql/07-phase9c1-incremental-capture-migration.sql",
    ROOT / "deploy/phase0/sql/08-phase9c2-chunk-manifest-migration.sql",
    ROOT / "deploy/phase0/sql/09-phase9c3-shadow-plans-migration.sql",
    ROOT / "deploy/phase0/sql/10-phase9c35-run-state-observability-migration.sql",
    ROOT / "deploy/phase0/sql/11-phase9c4-production-replacement-migration.sql",
]
COLLECTION = "phase9c4_e2e"
MODEL = "nomic-ai/nomic-embed-text-v1.5"


class Embedder(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        seed = int(hashlib.sha256(body.get("text", "").encode()).hexdigest()[:8], 16)
        vector = [((seed + index) % 1000) / 1000 for index in range(768)]
        payload = json.dumps({"embedding": vector, "model": MODEL, "dimension": 768}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    schema = f"phase9c4_e2e_{uuid.uuid4().hex[:12]}"
    db = psycopg.connect(database_url, autocommit=True)
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection(COLLECTION, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Embedder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        db.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        db.execute(psycopg.sql.SQL("SET search_path TO {}, public").format(psycopg.sql.Identifier(schema)))
        for migration in MIGRATIONS[:-1]:
            db.execute(migration.read_text(encoding="utf-8"))
        db.execute("CREATE TABLE rag_regression_runs (run_id UUID PRIMARY KEY,status TEXT,case_count INTEGER,pass_count INTEGER,fail_count INTEGER,review_count INTEGER)")
        db.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))
        baseline_digest = hashlib.sha256(b"empty").hexdigest()
        db.execute(
            """
            INSERT INTO rag_ingestion_runs
                (run_id,run_kind,status,collection_name,chunker_version,embedding_version,point_count,manifest_digest)
            VALUES ('baseline','baseline_seed','completed',%s,'v10',%s,0,%s)
            """,
            (COLLECTION, MODEL, baseline_digest),
        )
        db.execute(
            """
            INSERT INTO rag_corpus_versions
                (corpus_version_id,ingestion_run_id,collection_name,manifest_digest,point_count,status,activated_at)
            VALUES ('corpus-before','baseline',%s,%s,0,'healthy',now())
            """,
            (COLLECTION, baseline_digest),
        )
        for sequence, message_id in ((1, "910000000000000001"), (2, "910000000000000002")):
            db.execute(
                """
                INSERT INTO rag_discord_messages
                    (message_id,capture_sequence,guild_id,channel_id,channel_name,
                     parent_channel_id,parent_channel_name,author_id_hash,author_display_name,
                     content,message_created_at,message_type,normalizer_version)
                VALUES (%s,%s,'g','c','general','c','general','h','author',%s,
                        make_timestamptz(2026,8,1,12,%s,0,'UTC'),'default','v1')
                """,
                (message_id, sequence, f"message {sequence} useful content", sequence),
            )
            db.execute(
                """
                INSERT INTO rag_pending_chunk_work
                    (source_message_id,capture_sequence,work_kind,parent_channel_id)
                VALUES (%s,%s,'recent_window','c')
                """,
                (message_id, sequence),
            )
        work, live, manifest, source = load_postgres(db, collection=COLLECTION)
        plan = create_shadow_plan(work, live, manifest, [], collection=COLLECTION, source_corpus=source)
        embedder_url = f"http://127.0.0.1:{server.server_port}"
        rendered = render_plan(plan, embed_shadow(plan, embedder_url), source_corpus_current=True)
        assert rendered["validation"]["status"] == "shadow_validated"
        persist_plan(db, rendered)
        run_id = "phase9c4-e2e-run"
        db.execute("SELECT * FROM rag_create_incremental_run(%s,%s,%s,'{}')", (run_id, rendered["plan_id"], COLLECTION))
        db.execute("SELECT * FROM rag_begin_incremental_drain(%s,0)", (run_id,))
        db.execute("SELECT * FROM rag_enter_incremental_maintenance(%s,1)", (run_id,))
        db.execute("SELECT * FROM rag_mark_incremental_replacing(%s,2)", (run_id,))
        with tempfile.TemporaryDirectory() as exports:
            try:
                apply_replacement(
                    db, qdrant, run_id, exports, embedder_url,
                    fail_after_step="upsert",
                )
            except ExecutionError as error:
                assert "injected failure" in str(error)
            else:
                raise AssertionError("failure injection did not fail")
            assert qdrant.get_collection(COLLECTION).points_count == 0
            assert db.execute("SELECT count(*) FROM rag_pending_chunk_work WHERE status='pending'").fetchone()[0] == 2
            assert db.execute("SELECT run_state FROM rag_incremental_runs WHERE incremental_run_id=%s", (run_id,)).fetchone()[0] == "maintenance"
            db.execute("SELECT * FROM rag_mark_incremental_replacing(%s,2)", (run_id,))
            result = apply_replacement(db, qdrant, run_id, exports, embedder_url)
        assert result["status"] == "validating"
        assert qdrant.get_collection(COLLECTION).points_count == 1
        assert db.execute("SELECT count(*) FROM rag_chunk_manifest WHERE active").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM rag_pending_chunk_work WHERE status='completed'").fetchone()[0] == 2
        rolled_back = rollback_replacement(db, qdrant, run_id)
        assert rolled_back["status"] == "rolled_back"
        assert qdrant.get_collection(COLLECTION).points_count == 0
        assert db.execute("SELECT count(*) FROM rag_chunk_manifest WHERE active").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM rag_pending_chunk_work WHERE status='pending'").fetchone()[0] == 2
        assert db.execute("SELECT status FROM rag_corpus_versions WHERE corpus_version_id='corpus-before'").fetchone()[0] == "healthy"
        print("phase9c4 apply/rollback e2e checks passed")
    finally:
        server.shutdown()
        server.server_close()
        db.execute("RESET search_path")
        db.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))
        db.close()


if __name__ == "__main__":
    main()
