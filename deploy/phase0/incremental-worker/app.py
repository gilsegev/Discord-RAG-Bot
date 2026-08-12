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


def authorize(value: str | None) -> None:
    if WORKER_TOKEN and value != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid worker token")


@app.get("/health")
def health():
    return {"status": "ok", "qdrant_url": QDRANT_URL, "exports": EXPORT_DIR}


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
