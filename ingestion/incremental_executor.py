"""Phase 9C.4 deterministic production replacement and rollback data plane.

n8n owns run lifecycle transitions. This module performs only the bounded
apply/verify/rollback operation for a run that already owns maintenance.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ingestion.chunk_manifest import (
    ManifestRow,
    _canonical_digest,
    build_message_index,
    point_to_manifest,
    scan_qdrant,
)
from ingestion.incremental_planner import (
    PlanningError,
    create_shadow_plan,
    load_postgres,
    render_plan,
)
from ingestion.parser import parse_all_exports


class ExecutionError(RuntimeError):
    """The replacement cannot be safely applied or verified."""


def _embed(text: str, embedder_url: str) -> tuple[list[float], str]:
    request = urllib.request.Request(
        embedder_url.rstrip("/") + "/embed",
        data=json.dumps({"text": "search_document: " + text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    vector = [float(value) for value in result.get("embedding", [])]
    model = str(result.get("model") or "")
    if len(vector) != 768:
        raise ExecutionError(f"embedder returned {len(vector)} dimensions")
    return vector, model


def _run_and_plan(connection: Any, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = connection.execute(
        """
        SELECT r.incremental_run_id,r.plan_id,r.collection_name,r.run_state,
               r.batch_cutoff_sequence,r.corpus_version_before,
               p.status,p.plan_digest,p.source_corpus_version_id,
               p.source_manifest_digest,p.evidence,
               rs.runtime_state,rs.active_incremental_run_id,rs.state_revision
        FROM rag_incremental_runs r
        JOIN rag_chunk_replacement_plans p ON p.plan_id=r.plan_id
        JOIN rag_runtime_state rs ON rs.collection_name=r.collection_name
        WHERE r.incremental_run_id=%s
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ExecutionError(f"unknown incremental run {run_id}")
    keys = (
        "incremental_run_id", "plan_id", "collection_name", "run_state",
        "batch_cutoff_sequence", "corpus_version_before", "plan_status",
        "plan_digest", "source_corpus_version_id", "source_manifest_digest",
        "evidence", "runtime_state", "active_incremental_run_id",
        "runtime_revision",
    )
    state = dict(zip(keys, row))
    if state["plan_status"] != "shadow_validated":
        raise ExecutionError("only shadow_validated plans may be applied")
    return state, dict(state["evidence"])


def reconstruct(
    connection: Any,
    qdrant: Any,
    run_id: str,
    exports: str,
    embedder_url: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[float]], list[dict[str, Any]]]:
    """Rebuild and fully attest persisted plan material without mutation."""
    state, persisted = _run_and_plan(connection, run_id)
    work, live, manifest, source_corpus = load_postgres(
        connection,
        cutoff=int(state["batch_cutoff_sequence"]),
        collection=state["collection_name"],
        work_statuses=("pending", "claimed"),
    )
    records = parse_all_exports(exports) + live
    points = scan_qdrant(qdrant, state["collection_name"])
    plan = create_shadow_plan(
        work,
        records,
        manifest,
        points,
        collection=state["collection_name"],
        source_corpus=source_corpus,
    )
    if plan["plan_id"] != state["plan_id"] or plan["plan_digest"] != state["plan_digest"]:
        raise ExecutionError("reconstructed plan identity differs from persisted plan")

    vectors: dict[str, list[float]] = {}
    models: set[str] = set()
    started = time.perf_counter()
    for rows in plan["_replacement_details"].values():
        for row in rows:
            vector, model = _embed(row["text"], embedder_url)
            vectors[row["point_id"]] = vector
            models.add(model)
    measurement = {
        "measurement_kind": "shadow_replacements",
        "embedded_chunk_count": len(vectors),
        "embedding_dimensions": [768] if vectors else [],
        "observed_embedding_models": sorted(models),
        "measured_embedding_seconds": round(time.perf_counter() - started, 3),
    }
    rendered = render_plan(plan, measurement, source_corpus_current=True)
    if rendered["validation"]["status"] != "shadow_validated":
        raise ExecutionError(f"replacement evidence failed: {rendered['validation']}")
    for key in ("plan_id", "plan_digest", "groups", "replacement_point_count", "old_point_count"):
        if rendered.get(key) != persisted.get(key):
            raise ExecutionError(f"persisted plan evidence differs at {key}")
    return state, plan, vectors, records


def _snapshot_rows(connection: Any, run_id: str, old_ids: list[str]) -> list[dict[str, Any]]:
    if not old_ids:
        return []
    rows = connection.execute(
        """
        SELECT m.point_id,m.collection_name,m.logical_group_id,m.channel_id,
               m.thread_id,m.root_message_id,m.message_ids,m.first_message_id,
               m.last_message_id,m.chunker_version,m.embedding_version,
               m.ingestion_run_id,m.payload_digest,m.active,m.superseded_at,
               coalesce(jsonb_agg(jsonb_build_object(
                   'message_id',o.message_id,'message_position',o.message_position
               ) ORDER BY o.message_position) FILTER (WHERE o.message_id IS NOT NULL),'[]')
        FROM rag_chunk_manifest m
        LEFT JOIN rag_chunk_message_ownership o ON o.point_id=m.point_id
        WHERE m.point_id=ANY(%s)
        GROUP BY m.point_id
        """,
        (old_ids,),
    ).fetchall()
    result = []
    for row in rows:
        manifest = {
            "point_id": row[0], "collection_name": row[1],
            "logical_group_id": row[2], "channel_id": row[3],
            "thread_id": row[4], "root_message_id": row[5],
            "message_ids": row[6], "first_message_id": row[7],
            "last_message_id": row[8], "chunker_version": row[9],
            "embedding_version": row[10], "ingestion_run_id": row[11],
            "payload_digest": row[12], "active": row[13],
            "superseded_at": row[14].isoformat() if row[14] else None,
        }
        result.append({"manifest": manifest, "ownership": row[15]})
    if len(result) != len(set(old_ids)):
        raise ExecutionError("old point manifest snapshot is incomplete")
    return result


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _store_snapshots(
    connection: Any,
    run_id: str,
    points: list[Any],
    manifests: list[dict[str, Any]],
) -> tuple[int, str]:
    retained = datetime.now(timezone.utc) + timedelta(days=14)
    serial = []
    with connection.transaction():
        for point in points:
            vector = list(point.vector or [])
            payload = dict(point.payload or {})
            connection.execute(
                """
                INSERT INTO rag_incremental_point_snapshots
                    (incremental_run_id,point_id,vector,payload,payload_digest,retained_until)
                VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                ON CONFLICT (incremental_run_id,point_id) DO UPDATE SET
                    vector=EXCLUDED.vector,payload=EXCLUDED.payload,
                    payload_digest=EXCLUDED.payload_digest,
                    retained_until=GREATEST(rag_incremental_point_snapshots.retained_until,EXCLUDED.retained_until)
                """,
                (run_id, str(point.id), json.dumps(vector), json.dumps(payload),
                 _payload_digest(payload), retained),
            )
        for item in manifests:
            connection.execute(
                """
                INSERT INTO rag_incremental_manifest_snapshots
                    (incremental_run_id,point_id,manifest_row,ownership_rows,retained_until)
                VALUES (%s,%s,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT (incremental_run_id,point_id) DO NOTHING
                """,
                (run_id, item["manifest"]["point_id"], json.dumps(item["manifest"]),
                 json.dumps(item["ownership"]), retained),
            )
    encoded = json.dumps(
        [{"id": str(p.id), "vector": list(p.vector or []), "payload": p.payload or {}}
         for p in points],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _replacement_material(plan: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}
    for group in plan["groups"]:
        groups[group["group_key"]] = group
        for row in plan["_replacement_details"].get(group["group_key"], []):
            existing = rows.get(row["point_id"])
            if existing and existing != row:
                raise ExecutionError(f"conflicting replacement point {row['point_id']}")
            rows[row["point_id"]] = row
    return rows, groups


def _new_manifest_rows(
    plan: dict[str, Any], records: list[dict[str, Any]], run_id: str
) -> list[ManifestRow]:
    index = build_message_index(records)
    rows, groups = _replacement_material(plan)
    result = []
    for group_key, group in groups.items():
        for row in plan["_replacement_details"].get(group_key, []):
            owned = point_to_manifest(row["point_id"], row["_payload"], index)
            result.append(replace(
                owned,
                logical_group_id=group_key,
                root_message_id=group.get("root_message_id"),
            ))
    return sorted(result, key=lambda value: int(value.point_id))


def _commit_database(
    connection: Any,
    state: dict[str, Any],
    plan: dict[str, Any],
    manifest_rows: list[ManifestRow],
    point_count: int,
    snapshot_bytes: int,
    snapshot_digest: str,
    snapshot_uri: str,
) -> str:
    run_id = state["incremental_run_id"]
    old_ids = sorted({value for group in plan["groups"] for value in group["old_point_ids"]}, key=int)
    new_ids = {row.point_id for row in manifest_rows}
    with connection.transaction():
        unaffected = connection.execute(
            """
            SELECT point_id,logical_group_id,channel_id,thread_id,root_message_id,
                   message_ids,first_message_id,last_message_id,payload_digest
            FROM rag_chunk_manifest WHERE active AND NOT (point_id=ANY(%s))
            """,
            (old_ids,),
        ).fetchall()
        serial = [{
            "point_id": row[0], "logical_group_id": row[1], "channel_id": row[2],
            "thread_id": row[3], "root_message_id": row[4], "message_ids": row[5],
            "first_message_id": row[6], "last_message_id": row[7],
            "payload_digest": row[8],
        } for row in unaffected]
        serial.extend(asdict(row) for row in manifest_rows)
        # Match the Phase 9C.2 canonical manifest shape exactly.
        serial = [{key: value for key, value in row.items() if key in {
            "point_id","logical_group_id","channel_id","thread_id","root_message_id",
            "message_ids","first_message_id","last_message_id","payload_digest"
        }} for row in serial]
        serial.sort(key=lambda row: int(row["point_id"]))
        manifest_digest = _canonical_digest(serial)
        corpus_version = f"incremental-{manifest_digest[:20]}"

        connection.execute(
            """
            INSERT INTO rag_ingestion_runs
                (run_id,run_kind,status,collection_name,chunker_version,
                 embedding_version,point_count,manifest_digest,started_at,completed_at)
            VALUES (%s,'incremental','completed',%s,%s,%s,%s,%s,now(),now())
            ON CONFLICT (run_id) DO UPDATE SET status='completed',point_count=EXCLUDED.point_count,
                manifest_digest=EXCLUDED.manifest_digest,completed_at=now()
            """,
            (run_id, state["collection_name"], plan["chunker_version"],
             plan["embedding_version"], point_count, manifest_digest),
        )
        if old_ids:
            connection.execute(
                """UPDATE rag_chunk_manifest SET active=false,superseded_at=now()
                   WHERE point_id=ANY(%s) AND NOT (point_id=ANY(%s))""",
                (old_ids, list(new_ids)),
            )
        for row in manifest_rows:
            values = asdict(row)
            connection.execute(
                """
                INSERT INTO rag_chunk_manifest
                    (point_id,collection_name,logical_group_id,channel_id,thread_id,
                     root_message_id,message_ids,first_message_id,last_message_id,
                     chunker_version,embedding_version,ingestion_run_id,payload_digest,active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
                ON CONFLICT (point_id) DO UPDATE SET
                    collection_name=EXCLUDED.collection_name,
                    logical_group_id=EXCLUDED.logical_group_id,channel_id=EXCLUDED.channel_id,
                    thread_id=EXCLUDED.thread_id,root_message_id=EXCLUDED.root_message_id,
                    message_ids=EXCLUDED.message_ids,first_message_id=EXCLUDED.first_message_id,
                    last_message_id=EXCLUDED.last_message_id,chunker_version=EXCLUDED.chunker_version,
                    embedding_version=EXCLUDED.embedding_version,
                    ingestion_run_id=EXCLUDED.ingestion_run_id,payload_digest=EXCLUDED.payload_digest,
                    active=true,superseded_at=NULL
                """,
                (row.point_id,state["collection_name"],row.logical_group_id,row.channel_id,
                 row.thread_id,row.root_message_id,list(row.message_ids),row.first_message_id,
                 row.last_message_id,plan["chunker_version"],plan["embedding_version"],
                 run_id,row.payload_digest),
            )
            connection.execute("DELETE FROM rag_chunk_message_ownership WHERE point_id=%s", (row.point_id,))
            for position, message_id in enumerate(row.message_ids):
                connection.execute(
                    "INSERT INTO rag_chunk_message_ownership(point_id,message_id,message_position) VALUES (%s,%s,%s)",
                    (row.point_id, message_id, position),
                )
        connection.execute(
            "UPDATE rag_corpus_versions SET status='superseded',superseded_at=now() WHERE corpus_version_id=%s AND status='healthy'",
            (state["corpus_version_before"],),
        )
        connection.execute(
            """
            INSERT INTO rag_corpus_versions
                (corpus_version_id,ingestion_run_id,collection_name,manifest_digest,
                 point_count,status,activated_at)
            VALUES (%s,%s,%s,%s,%s,'healthy',now())
            ON CONFLICT (corpus_version_id) DO UPDATE SET status='healthy',activated_at=now(),superseded_at=NULL
            """,
            (corpus_version,run_id,state["collection_name"],manifest_digest,point_count),
        )
        ready_ids = sorted({value for group in plan["groups"] if group["status"] == "ready" for value in group["source_message_ids"]})
        processed = connection.execute(
            """UPDATE rag_pending_chunk_work SET status='completed',completed_at=now(),failure_reason=NULL
               WHERE source_message_id=ANY(%s) AND status='claimed' RETURNING work_id""",
            (ready_ids,),
        ).fetchall()
        if len(processed) != len(ready_ids):
            raise ExecutionError("claimed pending-work count changed before commit")
        connection.execute(
            """
            UPDATE rag_incremental_runs SET run_state='validating',
                corpus_version_after=%s,processed_message_count=%s,
                deferred_message_count=%s,new_point_count=%s,
                reused_point_count=%s,deleted_point_count=%s,
                snapshot_bytes=%s,snapshot_digest=%s,snapshot_uri=%s,
                snapshot_retained_until=now()+interval '14 days',
                structural_verification_result='passed',updated_at=now()
            WHERE incremental_run_id=%s AND run_state='replacing'
            """,
            (corpus_version,len(processed),
             sum(len(g["source_message_ids"]) for g in plan["groups"] if g["status"] == "deferred"),
             len(new_ids - set(old_ids)),len(new_ids & set(old_ids)),
             len(set(old_ids) - new_ids),snapshot_bytes,snapshot_digest,snapshot_uri,run_id),
        )
    return corpus_version


def apply_replacement(
    connection: Any,
    qdrant: Any,
    run_id: str,
    exports: str,
    embedder_url: str,
    *,
    take_full_snapshot: bool = False,
    fail_after_step: str | None = None,
) -> dict[str, Any]:
    state, plan, vectors, records = reconstruct(
        connection, qdrant, run_id, exports, embedder_url
    )
    if state["run_state"] != "replacing":
        raise ExecutionError("apply requires replacing run state")
    rows, _ = _replacement_material(plan)
    old_ids = sorted({value for group in plan["groups"] for value in group["old_point_ids"]}, key=int)
    before_count = int(qdrant.get_collection(state["collection_name"]).points_count or 0)
    old_points = qdrant.retrieve(
        state["collection_name"], ids=[int(value) for value in old_ids],
        with_payload=True, with_vectors=True,
    ) if old_ids else []
    if len(old_points) != len(old_ids):
        raise ExecutionError("Qdrant recovery snapshot is missing old points")
    manifests = _snapshot_rows(connection, run_id, old_ids)
    snapshot_bytes, snapshot_digest = _store_snapshots(
        connection, run_id, old_points, manifests
    )
    full_snapshot_name = ""
    if take_full_snapshot:
        full_snapshot_name = str(qdrant.create_snapshot(state["collection_name"]).name)
    snapshot_uri = f"postgres://rag_incremental_point_snapshots/{run_id}"
    if full_snapshot_name:
        snapshot_uri += f";qdrant://{state['collection_name']}/{full_snapshot_name}"
    new_ids = set(rows)
    deleted_ids = set(old_ids) - new_ids
    mutated = False
    try:
        from qdrant_client.models import PointStruct
        points = [PointStruct(
            id=int(point_id), vector=vectors[point_id], payload=row["_payload"]
        ) for point_id, row in sorted(rows.items(), key=lambda item: int(item[0]))]
        if points:
            qdrant.upsert(state["collection_name"], points=points, wait=True)
            mutated = True
        if fail_after_step == "upsert":
            raise ExecutionError("injected failure after upsert")
        if deleted_ids:
            qdrant.delete(state["collection_name"], points_selector=[int(value) for value in deleted_ids], wait=True)
            mutated = True
        if fail_after_step == "delete":
            raise ExecutionError("injected failure after delete")
        expected_count = before_count - len(deleted_ids) + len(new_ids - set(old_ids))
        actual_count = int(qdrant.get_collection(state["collection_name"]).points_count or 0)
        if actual_count != expected_count:
            raise ExecutionError(f"Qdrant point count {actual_count} != {expected_count}")
        verified = qdrant.retrieve(
            state["collection_name"], ids=[int(value) for value in sorted(new_ids, key=int)],
            with_payload=True, with_vectors=False,
        ) if new_ids else []
        if len(verified) != len(new_ids):
            raise ExecutionError("replacement point verification is incomplete")
        for point in verified:
            expected = rows[str(point.id)]
            text_digest = hashlib.sha256(str((point.payload or {}).get("text", "")).encode()).hexdigest()
            if text_digest != expected["text_digest"]:
                raise ExecutionError(f"replacement point {point.id} text digest mismatch")
        if fail_after_step == "verify":
            raise ExecutionError("injected failure after verification")
        manifest_rows = _new_manifest_rows(plan, records, run_id)
        corpus_version = _commit_database(
            connection,state,plan,manifest_rows,actual_count,
            snapshot_bytes,snapshot_digest,snapshot_uri,
        )
        return {
            "incremental_run_id": run_id, "status": "validating",
            "corpus_version_after": corpus_version,
            "old_point_count": len(old_ids), "replacement_point_count": len(new_ids),
            "deleted_point_count": len(deleted_ids), "point_count": actual_count,
            "snapshot_uri": snapshot_uri, "snapshot_digest": snapshot_digest,
            "full_snapshot_name": full_snapshot_name,
        }
    except Exception as error:
        if mutated:
            rollback_replacement(connection, qdrant, run_id, automatic=True)
        raise ExecutionError(str(error)) from error


def rollback_replacement(
    connection: Any, qdrant: Any, run_id: str, *, automatic: bool = False
) -> dict[str, Any]:
    state, persisted = _run_and_plan(connection, run_id)
    if state["runtime_state"] != "maintenance" or state["active_incremental_run_id"] != run_id:
        raise ExecutionError("rollback requires run-owned maintenance")
    old_ids = {value for group in persisted["groups"] for value in group["old_point_ids"]}
    new_ids = {value for group in persisted["groups"] for value in group["replacement_point_ids"]}
    snapshots = connection.execute(
        "SELECT point_id,vector,payload FROM rag_incremental_point_snapshots WHERE incremental_run_id=%s",
        (run_id,),
    ).fetchall()
    if len(snapshots) != len(old_ids):
        raise ExecutionError("rollback snapshot set is incomplete")
    from qdrant_client.models import PointStruct
    if snapshots:
        qdrant.upsert(
            state["collection_name"],
            points=[PointStruct(id=int(row[0]),vector=row[1],payload=row[2]) for row in snapshots],
            wait=True,
        )
    remove_ids = new_ids - old_ids
    if remove_ids:
        qdrant.delete(
            state["collection_name"],
            points_selector=[int(value) for value in remove_ids], wait=True,
        )
    with connection.transaction():
        manifest_snapshots = connection.execute(
            "SELECT manifest_row,ownership_rows FROM rag_incremental_manifest_snapshots WHERE incremental_run_id=%s",
            (run_id,),
        ).fetchall()
        if len(manifest_snapshots) != len(old_ids):
            raise ExecutionError("rollback manifest snapshot set is incomplete")
        if new_ids - old_ids:
            connection.execute(
                "UPDATE rag_chunk_manifest SET active=false,superseded_at=now() WHERE point_id=ANY(%s)",
                (list(new_ids - old_ids),),
            )
        for manifest, ownership in manifest_snapshots:
            connection.execute(
                """
                UPDATE rag_chunk_manifest SET collection_name=%s,logical_group_id=%s,
                    channel_id=%s,thread_id=%s,root_message_id=%s,message_ids=%s,
                    first_message_id=%s,last_message_id=%s,chunker_version=%s,
                    embedding_version=%s,ingestion_run_id=%s,payload_digest=%s,
                    active=%s,superseded_at=%s WHERE point_id=%s
                """,
                (manifest["collection_name"],manifest["logical_group_id"],manifest["channel_id"],
                 manifest.get("thread_id"),manifest.get("root_message_id"),manifest["message_ids"],
                 manifest["first_message_id"],manifest["last_message_id"],manifest["chunker_version"],
                 manifest["embedding_version"],manifest["ingestion_run_id"],manifest["payload_digest"],
                 manifest["active"],manifest.get("superseded_at"),manifest["point_id"]),
            )
            connection.execute("DELETE FROM rag_chunk_message_ownership WHERE point_id=%s", (manifest["point_id"],))
            for owned in ownership:
                connection.execute(
                    "INSERT INTO rag_chunk_message_ownership(point_id,message_id,message_position) VALUES (%s,%s,%s)",
                    (manifest["point_id"],owned["message_id"],owned["message_position"]),
                )
        if state.get("corpus_version_before"):
            connection.execute(
                "UPDATE rag_corpus_versions SET status='review_needed' WHERE ingestion_run_id=%s AND status='healthy'",
                (run_id,),
            )
            connection.execute(
                "UPDATE rag_corpus_versions SET status='healthy',superseded_at=NULL WHERE corpus_version_id=%s",
                (state["corpus_version_before"],),
            )
        connection.execute(
            "UPDATE rag_pending_chunk_work SET status='pending',claimed_at=NULL,completed_at=NULL,failure_reason='incremental_rollback' WHERE source_message_id=ANY(%s)",
            ([value for group in persisted["groups"] if group["status"] == "ready" for value in group["source_message_ids"]],),
        )
        connection.execute(
            """UPDATE rag_incremental_runs SET run_state='maintenance',rollback_status='completed',
               rollback_reason=%s,structural_verification_result='rolled_back',updated_at=now()
               WHERE incremental_run_id=%s""",
            ("automatic_apply_failure" if automatic else "requested",run_id),
        )
    return {"incremental_run_id": run_id, "status": "rolled_back", "restored_point_count": len(old_ids)}
