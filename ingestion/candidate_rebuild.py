"""Build a non-serving Qdrant candidate corpus from a frozen source boundary.

This adapter deliberately has no default cutover path.  It assembles exports and
durable captures by Discord message ID, creates a *new* collection, verifies its
ownership plan, and records a candidate-only manifest.  Promotion remains a
separate, maintenance-gated Phase 9C operation after regression approval.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from ingestion.chunk_manifest import OwnershipError, create_plan, scan_qdrant, verify_plan
from ingestion.chunker import chunk_records
from ingestion.run import _stable_id


class CandidateRebuildError(ValueError):
    pass


CANONICAL_SOURCE_FIELDS = (
    "id", "channel_id", "channel", "thread_id", "thread_name", "parent_id",
    "author", "content", "timestamp", "has_attachment",
)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def normalize_source_record(record: dict[str, Any]) -> dict[str, Any]:
    """Map export and capture shapes to the same semantic source contract."""
    try:
        stamp = record.get("timestamp", record.get("message_created_at"))
        if isinstance(stamp, datetime):
            parsed = stamp
        else:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include an offset")
        timestamp = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "id": str(record["id"] if "id" in record else record["message_id"]),
            "channel_id": str(record["channel_id"]),
            "channel": str(record.get("channel", record.get("parent_channel_name", record.get("channel_name", "")))).lower(),
            "thread_id": _text(record.get("thread_id")),
            "thread_name": _text(record.get("thread_name")),
            "parent_id": _text(record.get("parent_id", record.get("parent_message_id"))),
            "author": str(record.get("author", record.get("author_display_name", ""))),
            "content": str(record.get("content", "")).strip(),
            "timestamp": timestamp,
            "has_attachment": bool(record.get("has_attachment", record.get("has_attachments", False))),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise CandidateRebuildError(f"invalid source record: {error}") from error
    return {field: result[field] for field in CANONICAL_SOURCE_FIELDS}


def union_records(
    exports: Iterable[dict[str, Any]], captures: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return a deterministic ID union; reject mismatched duplicate messages."""
    result: dict[str, dict[str, Any]] = {}
    for record in [*exports, *captures]:
        normalized = normalize_source_record(record)
        previous = result.get(normalized["id"])
        if previous is not None and previous != normalized:
            raise CandidateRebuildError(
                f"conflicting source records for message {normalized['id']}"
            )
        result[normalized["id"]] = normalized
    return sorted(result.values(), key=lambda row: (row["timestamp"], int(row["id"])))


def payload_for(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": chunk["text"], "start_ts": chunk["start_ts"],
        "end_ts": chunk["end_ts"], "channel": chunk["channel"],
        "channel_id": chunk["channel_id"], "thread_name": chunk.get("thread_name"),
        "authors": chunk["authors"], "message_count": chunk["message_count"],
        "message_ids": chunk["message_ids"], "first_message_id": chunk["first_message_id"],
        "root_message_id": chunk.get("root_message_id"), "token_count": chunk["token_count"],
        "span_days": chunk["span_days"], "split_index": chunk.get("split_index", 0),
    }


def plan_candidate(
    exports: Iterable[dict[str, Any]], captures: Iterable[dict[str, Any]],
    *, candidate_collection: str, frozen_capture_sequence: int,
    frozen_at: datetime | None = None,
    chunker_version: str = "v11", embedding_version: str = "nomic-ai/nomic-embed-text-v1.5",
) -> dict[str, Any]:
    exports = list(exports)
    captures = list(captures)
    if frozen_at is not None:
        if frozen_at.tzinfo is None:
            raise CandidateRebuildError("frozen_at must include an offset")
        boundary = frozen_at.astimezone(timezone.utc)
        too_new = [row for row in exports if datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc) > boundary]
        if too_new:
            raise CandidateRebuildError("export rows exceed frozen timestamp")
    over_cutoff = [row for row in captures if row.get("capture_sequence") is not None
                   and int(row["capture_sequence"]) > frozen_capture_sequence]
    if over_cutoff:
        raise CandidateRebuildError("capture rows exceed frozen cutoff")
    records = union_records(exports, captures)
    chunks = chunk_records(records)
    points = [(str(_stable_id(chunk)), payload_for(chunk)) for chunk in chunks]
    try:
        manifest = create_plan(points, records, candidate_collection, chunker_version, embedding_version)
    except OwnershipError as error:
        raise CandidateRebuildError(str(error)) from error
    source_ids = {row["id"] for row in records}
    ownership = [message_id for row in manifest["rows"] for message_id in row["message_ids"]]
    owned_ids = set(ownership)
    uncovered = sorted(source_ids - owned_ids, key=int)
    duplicated = sorted((message_id for message_id, count in Counter(ownership).items() if count > 1), key=int)
    cross_channel = []
    by_id = {row["id"]: row for row in records}
    for point_id, payload in points:
        wrong = [mid for mid in payload["message_ids"]
                 if by_id[mid]["channel_id"] != str(payload["channel_id"])]
        if wrong:
            cross_channel.append({"point_id": point_id, "message_ids": wrong})
    if uncovered or duplicated or cross_channel:
        raise CandidateRebuildError(
            f"structural audit failed: uncovered={len(uncovered)}, duplicated={len(duplicated)}, cross_channel={len(cross_channel)}"
        )
    payload_digest = hashlib.sha256(json.dumps(sorted(points, key=lambda item: int(item[0])), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps({
        "collection": candidate_collection, "cutoff": frozen_capture_sequence,
        "frozen_at": frozen_at.astimezone(timezone.utc).isoformat() if frozen_at else None,
        "manifest": manifest["manifest_digest"], "payload": payload_digest,
        "embedding_version": embedding_version, "source_ids": sorted(source_ids, key=int),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**manifest, "candidate_id": f"candidate-{digest[:20]}",
            "frozen_capture_sequence": frozen_capture_sequence,
            "frozen_at": frozen_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if frozen_at else None,
            "source_message_count": len(source_ids), "source_message_digest": hashlib.sha256(
                "\n".join(sorted(source_ids, key=int)).encode()).hexdigest(),
            "candidate_payload_digest": payload_digest,
            "corpus_version_id": f"corpus-{digest[:20]}",
            "structural_audit": {"passed": True, "uncovered_message_ids": [], "duplicate_message_ids": [],
                                 "cross_channel_points": []}, "_points": points}


def load_captures(connection: Any, cutoff: int) -> list[dict[str, Any]]:
    rows = connection.execute("""
        SELECT message_id, channel_id, channel_name, parent_channel_name,
               thread_id, thread_name, parent_message_id, author_display_name,
               content, message_created_at, has_attachments, capture_sequence
        FROM rag_discord_messages WHERE capture_sequence <= %s
        ORDER BY message_created_at, message_id
    """, (cutoff,)).fetchall()
    return [{"id": str(r[0]), "channel_id": str(r[1]), "channel": r[3] or r[2],
             "thread_id": r[4], "thread_name": r[5], "parent_id": r[6],
             "author": r[7], "content": r[8], "timestamp": r[9],
             "has_attachment": bool(r[10]), "capture_sequence": int(r[11])} for r in rows]


def create_candidate_collection(client: Any, collection: str) -> None:
    """Create only a previously absent candidate collection; never replace one."""
    from qdrant_client.models import Distance, PayloadSchemaType, VectorParams
    if any(item.name == collection for item in client.get_collections().collections):
        raise CandidateRebuildError(f"candidate collection already exists: {collection}")
    client.create_collection(collection, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
    for field, schema in (("start_ts", PayloadSchemaType.DATETIME),
                          ("channel", PayloadSchemaType.KEYWORD),
                          ("thread_name", PayloadSchemaType.KEYWORD),
                          ("span_days", PayloadSchemaType.FLOAT)):
        client.create_payload_index(collection, field, schema)


def seed_candidate_metadata(connection: Any, plan: dict[str, Any]) -> None:
    """Persist candidate evidence only; it cannot alter the serving manifest."""
    connection.execute("""
        INSERT INTO rag_candidate_rebuilds
          (candidate_id,corpus_version_id,collection_name,frozen_capture_sequence,chunker_version,embedding_version,manifest_digest,candidate_payload_digest,
           point_count,source_message_count,source_message_digest,status,evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'built',%s::jsonb)
    """, (plan["candidate_id"], plan["corpus_version_id"], plan["collection_name"], plan["frozen_capture_sequence"],
          plan["chunker_version"], plan["embedding_version"], plan["manifest_digest"], plan["candidate_payload_digest"], plan["point_count"], plan["source_message_count"],
          plan["source_message_digest"], json.dumps({k: v for k, v in plan.items() if not k.startswith("_")})))
    for row in plan["rows"]:
        connection.execute("""
          INSERT INTO rag_candidate_chunk_manifest
            (candidate_id,corpus_version_id,collection_name,point_id,logical_group_id,channel_id,thread_id,root_message_id,
             message_ids,first_message_id,last_message_id,payload_digest)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (plan["candidate_id"], plan["corpus_version_id"], plan["collection_name"], row["point_id"], row["logical_group_id"], row["channel_id"],
              row["thread_id"], row["root_message_id"], row["message_ids"], row["first_message_id"],
              row["last_message_id"], row["payload_digest"]))


def embed_candidate(embedder_url: str, client: Any, plan: dict[str, Any], batch_size: int = 32) -> None:
    """Embed and synchronously upsert only into the plan's non-serving collection."""
    from qdrant_client.models import PointStruct
    collection = plan["collection_name"]
    for start in range(0, len(plan["_points"]), batch_size):
        batch = plan["_points"][start:start + batch_size]
        vectors = []
        for _, payload in batch:
            request = urllib.request.Request(embedder_url.rstrip("/") + "/embed",
                data=json.dumps({"text": "search_document: " + payload["text"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            vector = [float(value) for value in result.get("embedding", [])]
            if len(vector) != 768 or str(result.get("model") or "") != plan["embedding_version"]:
                raise CandidateRebuildError("embedder model or vector dimension does not match candidate contract")
            vectors.append(vector)
        client.upsert(collection, points=[PointStruct(id=int(point_id), vector=vector, payload=payload)
                                          for (point_id, payload), vector in zip(batch, vectors)], wait=True)
    actual = int(client.get_collection(collection).points_count or 0)
    if actual != plan["point_count"]:
        raise CandidateRebuildError(f"candidate point count {actual} != {plan['point_count']}")


def switch_serving_alias(client: Any, alias: str, previous: str | None, target: str) -> None:
    """Atomically move one Qdrant alias; callers persist the matching DB pointer."""
    from qdrant_client.models import CreateAliasOperation, CreateAlias, DeleteAlias, DeleteAliasOperation
    actions = []
    if previous is not None:
        actions.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=alias)))
    actions.append(CreateAliasOperation(create_alias=CreateAlias(collection_name=target, alias_name=alias)))
    client.update_collection_aliases(change_aliases_operations=actions)


def promote_candidate(connection: Any, client: Any, candidate_id: str, alias: str) -> dict[str, Any]:
    """Maintenance-gated saga: alias first, pointer commit second, alias compensation on DB failure."""
    evidence_row = connection.execute("SELECT collection_name,evidence FROM rag_candidate_rebuilds WHERE candidate_id=%s", (candidate_id,)).fetchone()
    if not evidence_row:
        raise CandidateRebuildError("unknown candidate")
    live_points = scan_qdrant(client, str(evidence_row[0]))
    try:
        verify_plan(evidence_row[1], live_points)
    except OwnershipError as error:
        raise CandidateRebuildError(f"candidate changed after validation: {error}") from error
    live_payload_digest = hashlib.sha256(json.dumps(sorted(live_points, key=lambda item: int(item[0])),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if live_payload_digest != evidence_row[1].get("candidate_payload_digest"):
        raise CandidateRebuildError("candidate payload changed after validation")
    row = connection.execute("SELECT * FROM rag_begin_candidate_promotion(%s,%s)", (candidate_id, alias)).fetchone()
    if not row:
        raise CandidateRebuildError("candidate promotion was not admitted")
    previous_collection, target_collection, promotion_id = str(row[0]), str(row[1]), str(row[2])
    connection.commit()  # make switching intent durable before any Qdrant mutation
    snapshot = client.create_snapshot(previous_collection)
    connection.execute("SELECT rag_record_candidate_snapshot(%s,%s)", (promotion_id, str(snapshot.name)))
    connection.commit()
    switch_serving_alias(client, alias, previous_collection if previous_collection != alias else None, target_collection)
    # A commit error is outcome-ambiguous. Never guess by moving the alias again;
    # leave the durable switching state for /candidate/reconcile.
    connection.execute("SELECT rag_commit_candidate_promotion(%s)", (promotion_id,))
    connection.commit()
    return {"promotion_id": promotion_id, "collection_name": target_collection, "snapshot_name": str(snapshot.name)}


def rollback_promotion(connection: Any, client: Any, promotion_id: str, alias: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM rag_begin_candidate_rollback(%s)", (promotion_id,)).fetchone()
    if not row:
        raise CandidateRebuildError("rollback was not admitted")
    current_collection, previous_collection, recorded_alias = str(row[0]), str(row[1]), str(row[2])
    connection.commit()  # make rollback intent durable before the alias mutation
    switch_serving_alias(client, recorded_alias, current_collection, previous_collection)
    connection.execute("SELECT rag_commit_candidate_rollback(%s)", (promotion_id,))
    connection.commit()
    return {"promotion_id": promotion_id, "collection_name": previous_collection, "status": "rolled_back"}


def reconcile_promotion(connection: Any, client: Any, promotion_id: str, alias: str) -> str:
    row = connection.execute("SELECT logical_name FROM rag_candidate_promotions WHERE promotion_id=%s", (promotion_id,)).fetchone()
    if not row:
        raise CandidateRebuildError("unknown promotion")
    alias = str(row[0])
    aliases = client.get_aliases().aliases
    targets = [item.collection_name for item in aliases if item.alias_name == alias]
    observed = targets[0] if len(targets) == 1 else ""
    result = connection.execute("SELECT rag_reconcile_candidate_switch(%s,%s)", (promotion_id, observed)).fetchone()
    connection.commit()
    return str(result[0])
