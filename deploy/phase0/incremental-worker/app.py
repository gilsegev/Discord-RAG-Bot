import os
import threading

import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from qdrant_client import QdrantClient

from ingestion.incremental_executor import (
    ExecutionError,
    apply_replacement,
    reconstruct,
    rollback_replacement,
)
from ingestion.chunk_manifest import scan_qdrant
from ingestion.incremental_planner import (
    PlanningError,
    create_shadow_plan,
    embed_shadow,
    load_postgres,
    persist_plan,
    render_plan,
)
from ingestion.parser import parse_all_exports


DATABASE_URL = os.environ["DATABASE_URL"]
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
EMBEDDER_URL = os.getenv("EMBEDDER_URL", "http://embedder:8000")
EXPORT_DIR = os.getenv("EXPORT_DIR", "/app/chat_logs")
WORKER_TOKEN = os.getenv("INCREMENTAL_WORKER_TOKEN", "")

app = FastAPI(title="Discord RAG Incremental Replacement Worker")
operation_lock = threading.Lock()


class RunRequest(BaseModel):
    incremental_run_id: str
    take_full_snapshot: bool = False
    fail_after_step: str | None = None


class PlanRequest(BaseModel):
    collection_name: str = "tpm_unite_history"
    batch_cutoff_sequence: int | None = None
    persist: bool = False


def authorize(value: str | None) -> None:
    if WORKER_TOKEN and value != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid worker token")


@app.get("/health")
def health():
    return {"status": "ok", "qdrant_url": QDRANT_URL, "exports": EXPORT_DIR}


@app.post("/plan")
def plan(request: PlanRequest, x_incremental_worker_token: str | None = Header(default=None)):
    """Build and optionally persist a shadow plan; never mutate Qdrant."""
    authorize(x_incremental_worker_token)
    if request.batch_cutoff_sequence is not None and request.batch_cutoff_sequence < 0:
        raise HTTPException(status_code=400, detail="batch cutoff must be non-negative")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        qdrant = QdrantClient(url=QDRANT_URL)
        with psycopg.connect(DATABASE_URL) as connection:
            work, live, manifest, source_corpus = load_postgres(
                connection,
                cutoff=request.batch_cutoff_sequence,
                collection=request.collection_name,
            )
            records = parse_all_exports(EXPORT_DIR) + live
            points = scan_qdrant(qdrant, request.collection_name)
            shadow = create_shadow_plan(
                work, records, manifest, points, request.collection_name,
                source_corpus=source_corpus,
            )
            measurement = (
                embed_shadow(shadow, EMBEDDER_URL)
                if shadow["replacement_point_count"] else None
            )
            rendered = render_plan(
                shadow, measurement, source_corpus_current=source_corpus is not None
            )
            if request.persist:
                persist_plan(connection, rendered)
        return {
            "status": rendered["validation"]["status"],
            "plan_id": rendered["plan_id"],
            "collection_name": rendered["collection_name"],
            "batch_cutoff_sequence": rendered["batch_cutoff_sequence"],
            "pending_message_count": rendered["pending_message_count"],
            "old_point_count": rendered["old_point_count"],
            "replacement_point_count": rendered["replacement_point_count"],
            "estimated_seconds": (rendered.get("measurement") or {}).get("measured_embedding_seconds"),
            "persisted": request.persist,
            "qdrant_mutations": 0,
            "validation": rendered["validation"],
        }
    except (PlanningError, ExecutionError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        operation_lock.release()


@app.post("/preflight")
def preflight(request: RunRequest, x_incremental_worker_token: str | None = Header(default=None)):
    authorize(x_incremental_worker_token)
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            state, plan, vectors, _ = reconstruct(
                connection, QdrantClient(url=QDRANT_URL),
                request.incremental_run_id, EXPORT_DIR, EMBEDDER_URL,
            )
        return {
            "status": "ready",
            "incremental_run_id": request.incremental_run_id,
            "plan_id": state["plan_id"],
            "old_point_count": plan["old_point_count"],
            "replacement_point_count": len(vectors),
            "qdrant_mutations": 0,
        }
    except ExecutionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        operation_lock.release()


@app.post("/apply")
def apply(request: RunRequest, x_incremental_worker_token: str | None = Header(default=None)):
    authorize(x_incremental_worker_token)
    if request.fail_after_step not in (None, "upsert", "delete", "verify"):
        raise HTTPException(status_code=400, detail="unsupported failure injection step")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            return apply_replacement(
                connection, QdrantClient(url=QDRANT_URL),
                request.incremental_run_id, EXPORT_DIR, EMBEDDER_URL,
                take_full_snapshot=request.take_full_snapshot,
                fail_after_step=request.fail_after_step,
            )
    except ExecutionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        operation_lock.release()


@app.post("/rollback")
def rollback(request: RunRequest, x_incremental_worker_token: str | None = Header(default=None)):
    authorize(x_incremental_worker_token)
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            return rollback_replacement(
                connection, QdrantClient(url=QDRANT_URL), request.incremental_run_id
            )
    except ExecutionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        operation_lock.release()
