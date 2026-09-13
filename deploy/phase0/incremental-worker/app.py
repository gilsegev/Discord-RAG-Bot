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
from ingestion.candidate_rebuild import (
    CandidateRebuildError, create_candidate_collection, embed_candidate,
    load_captures, plan_candidate, promote_candidate, rollback_promotion,
    reconcile_promotion, seed_candidate_metadata,
    switch_serving_alias,
)
from ingestion.incremental_planner import (
    PlanningError,
    create_shadow_plan,
    embed_shadow,
    load_postgres,
    persist_plan,
    resolve_active_corpus,
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
    collection_name: str = "rag_active"
    batch_cutoff_sequence: int | None = None
    persist: bool = False


class CandidateBuildRequest(BaseModel):
    collection_name: str
    frozen_capture_sequence: int


class CandidateTransitionRequest(BaseModel):
    candidate_id: str | None = None
    promotion_id: str | None = None
    logical_alias: str = "rag_active"


def authorize(value: str | None) -> None:
    if WORKER_TOKEN and value != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid worker token")


@app.get("/health")
def health():
    return {"status": "ok", "qdrant_url": QDRANT_URL, "exports": EXPORT_DIR}


@app.post("/candidate/build")
def candidate_build(request: CandidateBuildRequest, x_incremental_worker_token: str | None = Header(default=None)):
    """Build a new immutable physical collection without changing the serving alias."""
    authorize(x_incremental_worker_token)
    if request.collection_name == "rag_active" or request.frozen_capture_sequence < 0:
        raise HTTPException(status_code=400, detail="candidate requires a physical collection and non-negative cutoff")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    qdrant = None
    created = False
    try:
        qdrant = QdrantClient(url=QDRANT_URL)
        with psycopg.connect(DATABASE_URL) as connection:
            boundary = connection.execute("SELECT max(message_created_at) FROM rag_discord_messages WHERE capture_sequence<=%s", (request.frozen_capture_sequence,)).fetchone()[0]
            if boundary is None and request.frozen_capture_sequence:
                raise CandidateRebuildError("frozen cutoff has no captured boundary")
            plan = plan_candidate(parse_all_exports(EXPORT_DIR), load_captures(connection, request.frozen_capture_sequence),
                                  candidate_collection=request.collection_name,
                                  frozen_capture_sequence=request.frozen_capture_sequence, frozen_at=boundary)
            create_candidate_collection(qdrant, request.collection_name)
            created = True
            embed_candidate(EMBEDDER_URL, qdrant, plan)
            seed_candidate_metadata(connection, plan)
        return {k: v for k, v in plan.items() if not k.startswith("_")}
    except CandidateRebuildError as error:
        if created and qdrant is not None:
            qdrant.delete_collection(request.collection_name)
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        if created and qdrant is not None:
            qdrant.delete_collection(request.collection_name)
        raise
    finally:
        operation_lock.release()


@app.post("/candidate/bootstrap-alias")
def candidate_bootstrap_alias(request: CandidateTransitionRequest, x_incremental_worker_token: str | None = Header(default=None)):
    """One-time idempotent creation of the stable alias from the seeded pointer."""
    authorize(x_incremental_worker_token)
    qdrant = QdrantClient(url=QDRANT_URL)
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute("SELECT collection_name FROM rag_active_corpus WHERE logical_name=%s AND state='serving'", (request.logical_alias,)).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="active corpus pointer is not bootstrapped")
    matches = [a.collection_name for a in qdrant.get_aliases().aliases if a.alias_name == request.logical_alias]
    if matches and matches != [str(row[0])]:
        raise HTTPException(status_code=409, detail="existing alias disagrees with active corpus pointer")
    if not matches:
        switch_serving_alias(qdrant, request.logical_alias, None, str(row[0]))
    return {"logical_alias": request.logical_alias, "collection_name": str(row[0]), "status": "serving"}


@app.post("/candidate/promote")
def candidate_promote(request: CandidateTransitionRequest, x_incremental_worker_token: str | None = Header(default=None)):
    authorize(x_incremental_worker_token)
    if not request.candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            return promote_candidate(connection, QdrantClient(url=QDRANT_URL), request.candidate_id, request.logical_alias)
    finally:
        operation_lock.release()


@app.post("/candidate/rollback")
def candidate_rollback(request: CandidateTransitionRequest, x_incremental_worker_token: str | None = Header(default=None)):
    authorize(x_incremental_worker_token)
    if not request.promotion_id:
        raise HTTPException(status_code=400, detail="promotion_id is required")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            return rollback_promotion(connection, QdrantClient(url=QDRANT_URL), request.promotion_id, request.logical_alias)
    finally:
        operation_lock.release()


@app.post("/candidate/reconcile")
def candidate_reconcile(request: CandidateTransitionRequest, x_incremental_worker_token: str | None = Header(default=None)):
    authorize(x_incremental_worker_token)
    if not request.promotion_id and not request.candidate_id:
        raise HTTPException(status_code=400, detail="promotion_id or candidate_id is required")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="incremental operation already running")
    try:
        with psycopg.connect(DATABASE_URL) as connection:
            promotion_id = request.promotion_id
            if not promotion_id:
                row = connection.execute("SELECT promotion_id FROM rag_candidate_promotions WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1", (request.candidate_id,)).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="candidate has no promotion")
                promotion_id = str(row[0])
            status = reconcile_promotion(connection, QdrantClient(url=QDRANT_URL), promotion_id, request.logical_alias)
        return {"promotion_id": promotion_id, "status": status}
    finally:
        operation_lock.release()


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
            try:
                target = resolve_active_corpus(connection, request.collection_name)
            except PlanningError:
                target = None
            physical_collection = target["collection_name"] if target else request.collection_name
            planning_collection = "rag_active" if target else request.collection_name
            work, live, manifest, source_corpus = load_postgres(
                connection,
                cutoff=request.batch_cutoff_sequence,
                collection=physical_collection,
            )
            if source_corpus is not None and target is not None:
                source_corpus = {**source_corpus, "active_revision": target["active_revision"], "logical_name": request.collection_name}
            records = parse_all_exports(EXPORT_DIR) + live
            points = scan_qdrant(qdrant, physical_collection)
            shadow = create_shadow_plan(
                work, records, manifest, points, planning_collection,
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
