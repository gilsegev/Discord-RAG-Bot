"""Build a non-serving Qdrant candidate corpus from a frozen source boundary.

This adapter deliberately has no default cutover path.  It assembles exports and
durable captures by Discord message ID, creates a *new* collection, verifies its
ownership plan, and records a candidate-only manifest.  Promotion remains a
separate, maintenance-gated Phase 9C operation after regression approval.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Iterable

from ingestion.chunk_manifest import OwnershipError, create_plan
from ingestion.chunker import chunk_records
from ingestion.run import _stable_id


class CandidateRebuildError(ValueError):
    pass


def union_records(
    exports: Iterable[dict[str, Any]], captures: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return a deterministic ID union; reject mismatched duplicate messages."""
    result: dict[str, dict[str, Any]] = {}
    for record in [*exports, *captures]:
        normalized = {**record, "id": str(record["id"]),
                      "channel_id": str(record["channel_id"])}
        previous = result.get(normalized["id"])
        if previous is not None and previous != normalized:
            raise CandidateRebuildError(
                f"conflicting source records for message {normalized['id']}"
            )
        result[normalized["id"]] = normalized
    return sorted(result.values(), key=lambda row: (row["timestamp"], row["id"]))


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
    chunker_version: str = "v11", embedding_version: str = "nomic-ai/nomic-embed-text-v1.5",
) -> dict[str, Any]:
    records = union_records(exports, captures)
    chunks = chunk_records(records)
    points = [(str(_stable_id(chunk)), payload_for(chunk)) for chunk in chunks]
    try:
        manifest = create_plan(points, records, candidate_collection, chunker_version, embedding_version)
    except OwnershipError as error:
        raise CandidateRebuildError(str(error)) from error
    source_ids = {row["id"] for row in records}
    owned_ids = {message_id for row in manifest["rows"] for message_id in row["message_ids"]}
    uncovered = sorted(source_ids - owned_ids, key=int)
    cross_channel = []
    by_id = {row["id"]: row for row in records}
    for point_id, payload in points:
        wrong = [mid for mid in payload["message_ids"]
                 if by_id[mid]["channel_id"] != str(payload["channel_id"])]
        if wrong:
            cross_channel.append({"point_id": point_id, "message_ids": wrong})
    if uncovered or cross_channel:
        raise CandidateRebuildError(
            f"structural audit failed: uncovered={len(uncovered)}, cross_channel={len(cross_channel)}"
        )
    digest = hashlib.sha256(json.dumps({
        "collection": candidate_collection, "cutoff": frozen_capture_sequence,
        "manifest": manifest["manifest_digest"], "source_ids": sorted(source_ids, key=int),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**manifest, "candidate_id": f"candidate-{digest[:20]}",
            "frozen_capture_sequence": frozen_capture_sequence,
            "source_message_count": len(source_ids), "source_message_digest": hashlib.sha256(
                "\n".join(sorted(source_ids, key=int)).encode()).hexdigest(),
            "structural_audit": {"passed": True, "uncovered_message_ids": [],
                                 "cross_channel_points": []}, "_points": points}


def load_captures(connection: Any, cutoff: int) -> list[dict[str, Any]]:
    rows = connection.execute("""
        SELECT message_id, channel_id, channel_name, parent_channel_name,
               thread_id, thread_name, parent_message_id, author_display_name,
               content, message_created_at
        FROM rag_discord_messages WHERE capture_sequence <= %s
        ORDER BY message_created_at, message_id
    """, (cutoff,)).fetchall()
    return [{"id": str(r[0]), "channel_id": str(r[1]), "channel": r[3],
             "thread_id": r[4], "thread_name": r[5], "parent_id": r[6],
             "author": r[7], "content": r[8], "timestamp": r[9].isoformat()} for r in rows]


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
          (candidate_id,collection_name,frozen_capture_sequence,manifest_digest,
           point_count,source_message_count,source_message_digest,status,evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'built',%s::jsonb)
    """, (plan["candidate_id"], plan["collection_name"], plan["frozen_capture_sequence"],
          plan["manifest_digest"], plan["point_count"], plan["source_message_count"],
          plan["source_message_digest"], json.dumps({k: v for k, v in plan.items() if not k.startswith("_")})))
    for row in plan["rows"]:
        connection.execute("""
          INSERT INTO rag_candidate_chunk_manifest
            (candidate_id,point_id,logical_group_id,channel_id,thread_id,root_message_id,
             message_ids,first_message_id,last_message_id,payload_digest)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (plan["candidate_id"], row["point_id"], row["logical_group_id"], row["channel_id"],
              row["thread_id"], row["root_message_id"], row["message_ids"], row["first_message_id"],
              row["last_message_id"], row["payload_digest"]))
