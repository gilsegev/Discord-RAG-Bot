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
    payload_duplicates = [
        {"point_id": point_id, "message_ids": message_ids}
        for point_id, payload in points
        if (message_ids := payload["message_ids"])
        and len(message_ids) != len(set(message_ids))
    ]
    cross_channel = []
    by_id = {row["id"]: row for row in records}
    for point_id, payload in points:
        wrong = [mid for mid in payload["message_ids"]
                 if by_id[mid]["channel_id"] != str(payload["channel_id"])]
        if wrong:
            cross_channel.append({"point_id": point_id, "message_ids": wrong})
    if uncovered or payload_duplicates or cross_channel:
        raise CandidateRebuildError(
            f"structural audit failed: uncovered={len(uncovered)}, payload_duplicates={len(payload_duplicates)}, cross_channel={len(cross_channel)}"
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
            "vector_size": 768, "vector_distance": "Cosine",
            "corpus_version_id": f"corpus-{digest[:20]}",
            "structural_audit": {"passed": True, "uncovered_message_ids": [], "payload_duplicate_message_ids": [],
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


def admit_frozen_cutoff(connection: Any, requested: int | None) -> tuple[int, datetime | None]:
    row = connection.execute("SELECT coalesce(max(capture_sequence),0),max(message_created_at) FROM rag_discord_messages").fetchone()
    observed = int(row[0])
    if requested is not None and requested != observed:
        raise CandidateRebuildError(f"requested cutoff {requested} != observed maximum {observed}")
    return observed, row[1]


def require_final_cutoff(candidate_cutoff: int, observed_cutoff: int) -> None:
    if candidate_cutoff != observed_cutoff:
        raise CandidateRebuildError(
            f"candidate cutoff {candidate_cutoff} is not final observed cutoff {observed_cutoff}; catch up and rerun regression"
        )


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
          (candidate_id,corpus_version_id,collection_name,frozen_capture_sequence,frozen_at,chunker_version,embedding_version,vector_size,vector_distance,manifest_digest,candidate_payload_digest,
           point_count,source_message_count,source_message_digest,status,evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'built',%s::jsonb)
    """, (plan["candidate_id"], plan["corpus_version_id"], plan["collection_name"], plan["frozen_capture_sequence"],
          plan["frozen_at"], plan["chunker_version"], plan["embedding_version"], plan["vector_size"], plan["vector_distance"], plan["manifest_digest"], plan["candidate_payload_digest"], plan["point_count"], plan["source_message_count"],
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
            request = urllib.request.Request(_embedding_endpoint(embedder_url),
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


def _embedding_endpoint(configured_url: str) -> str:
    """Accept either the production full endpoint or a legacy service base URL."""
    normalized = configured_url.rstrip("/")
    return normalized if normalized.endswith("/embed") else normalized + "/embed"
