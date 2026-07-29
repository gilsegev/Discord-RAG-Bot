"""End-to-end Phase 9C.3.5 rechunk simulation against real Postgres.

The test uses an isolated schema and a fixture embedder. It exercises canonical
capture rows -> shadow rechunking -> complete validation -> durable plan ->
durable run-summary persistence. Qdrant is represented by an empty read-only
point set and is never called or mutated.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
]
COLLECTION = "phase9c35_simulation"
CORPUS_VERSION = "phase9c35-simulation-baseline"
MANIFEST_DIGEST = hashlib.sha256(b"phase9c35-empty-baseline").hexdigest()


class FixtureEmbedder(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/embed":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        json.loads(self.rfile.read(length) or b"{}")
        payload = json.dumps(
            {
                "embedding": [0.0] * 768,
                "model": "nomic-ai/nomic-embed-text-v1.5",
                "dimension": 768,
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def use_schema(connection: psycopg.Connection, schema: str) -> None:
    connection.execute(
        psycopg.sql.SQL("SET search_path TO {}, public").format(
            psycopg.sql.Identifier(schema)
        )
    )


def seed_simulation(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        INSERT INTO rag_ingestion_runs (
            run_id, run_kind, status, collection_name, chunker_version,
            embedding_version, point_count, manifest_digest, completed_at
        ) VALUES (
            'phase9c35-simulation-baseline', 'baseline_seed', 'completed',
            %s, 'v10', 'nomic-ai/nomic-embed-text-v1.5', 0, %s, now()
        )
        """,
        (COLLECTION, MANIFEST_DIGEST),
    )
    connection.execute(
        """
        INSERT INTO rag_corpus_versions (
            corpus_version_id, ingestion_run_id, collection_name,
            manifest_digest, point_count, status, activated_at
        ) VALUES (%s, 'phase9c35-simulation-baseline', %s, %s, 0, 'healthy', now())
        """,
        (CORPUS_VERSION, COLLECTION, MANIFEST_DIGEST),
    )
    for sequence, message_id, minute in (
        (1, "910000000000000001", 0),
        (2, "910000000000000002", 1),
    ):
        connection.execute(
            """
            INSERT INTO rag_discord_messages (
                message_id, capture_sequence, guild_id, channel_id,
                channel_name, parent_channel_id, parent_channel_name,
                author_id_hash, author_display_name, content,
                message_created_at, message_type, normalizer_version
            ) VALUES (
                %s, %s, 'guild-simulation', 'channel-simulation',
                'simulation', 'channel-simulation', 'simulation',
                'fixture-author-hash', 'fixture-author', %s,
                make_timestamptz(2026, 7, 28, 12, %s, 0, 'UTC'),
                'default', 'discord-export-compatible-v1'
            )
            """,
            (
                message_id,
                sequence,
                f"Simulation message {sequence} with useful rechunk content",
                minute,
            ),
        )
        connection.execute(
            """
            INSERT INTO rag_pending_chunk_work (
                source_message_id, capture_sequence, work_kind,
                parent_channel_id, status
            ) VALUES (%s, %s, 'recent_window', 'channel-simulation', 'pending')
            """,
            (message_id, sequence),
        )


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    schema = f"phase9c35_e2e_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(database_url, autocommit=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureEmbedder)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        admin.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(
                psycopg.sql.Identifier(schema)
            )
        )
        use_schema(admin, schema)
        for migration in MIGRATIONS:
            admin.execute(migration.read_text(encoding="utf-8"))
        seed_simulation(admin)

        work, live, manifest, source_corpus = load_postgres(
            admin, collection=COLLECTION
        )
        before = {
            "pending": admin.execute(
                "SELECT count(*) FROM rag_pending_chunk_work WHERE status='pending'"
            ).fetchone()[0],
            "manifest": admin.execute(
                "SELECT count(*) FROM rag_chunk_manifest WHERE active"
            ).fetchone()[0],
        }
        plan = create_shadow_plan(
            work,
            live,
            manifest,
            [],
            collection=COLLECTION,
            source_corpus=source_corpus,
        )
        measurement = embed_shadow(
            plan, f"http://127.0.0.1:{server.server_port}"
        )
        rendered = render_plan(
            plan,
            measurement,
            source_corpus_current=True,
        )
        assert rendered["validation"]["status"] == "shadow_validated", rendered[
            "validation"
        ]
        persist_plan(admin, rendered)

        run_id = f"simulation-{rendered['plan_id']}"
        created = admin.execute(
            """
            SELECT * FROM rag_create_incremental_run(
                %s, %s, %s,
                '{"simulation":true,"qdrant_mutations":0,"pending_work_claimed":0}'
            )
            """,
            (
                run_id,
                rendered["plan_id"],
                COLLECTION,
            ),
        ).fetchone()
        assert created[1:4] == ("created", "serving", 0)

        # Reconnect before verification to prove the evidence is committed and
        # not merely visible inside the writer's session.
        verifier = psycopg.connect(database_url, autocommit=True)
        try:
            use_schema(verifier, schema)
            after = {
                "pending": verifier.execute(
                    "SELECT count(*) FROM rag_pending_chunk_work WHERE status='pending'"
                ).fetchone()[0],
                "manifest": verifier.execute(
                    "SELECT count(*) FROM rag_chunk_manifest WHERE active"
                ).fetchone()[0],
            }
            durable_run = verifier.execute(
                """
                SELECT run_state, collection_name, pending_message_count,
                       replacement_point_count, run_metadata
                FROM rag_incremental_runs
                WHERE incremental_run_id=%s
                """,
                (run_id,),
            ).fetchone()
            runtime = verifier.execute(
                """
                SELECT runtime_state, active_incremental_run_id, state_revision
                FROM rag_runtime_state WHERE collection_name=%s
                """,
                (COLLECTION,),
            ).fetchone()
        finally:
            verifier.close()

        assert before == after == {"pending": 2, "manifest": 0}
        assert durable_run[:4] == ("created", COLLECTION, 2, 1)
        assert durable_run[4]["simulation"] is True
        assert runtime == ("serving", None, 0)

        print(
            json.dumps(
                {
                    "schema": schema,
                    "plan_id": rendered["plan_id"],
                    "plan_status": rendered["validation"]["status"],
                    "replacement_point_count": rendered[
                        "replacement_point_count"
                    ],
                    "embedded_chunk_count": measurement["embedded_chunk_count"],
                    "qdrant_mutations": rendered["qdrant_mutations"],
                    "pending_before_after": [before["pending"], after["pending"]],
                    "manifest_before_after": [
                        before["manifest"],
                        after["manifest"],
                    ],
                    "incremental_run_id": run_id,
                    "runtime_state": runtime[0],
                    "runtime_revision": runtime[2],
                },
                sort_keys=True,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        admin.execute("RESET search_path")
        admin.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(schema)
            )
        )
        admin.close()


if __name__ == "__main__":
    main()
