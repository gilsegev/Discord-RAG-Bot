"""Phase 9c.2 baseline chunk ownership planning and verification.

The default command is read-only: Qdrant is scrolled and a deterministic JSON
plan is written to stdout or ``--output``. ``--apply`` is the only mode that
writes, and it writes Postgres manifest state; this module never mutates Qdrant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from ingestion.parser import parse_all_exports
from ingestion.run import _stable_id

DEFAULT_COLLECTION = "tpm_unite_history"
PLAN_VERSION = 1


class OwnershipError(ValueError):
    """The baseline cannot be assigned complete, unambiguous ownership."""


@dataclass(frozen=True)
class ManifestRow:
    point_id: str
    logical_group_id: str
    channel_id: str
    thread_id: str | None
    root_message_id: str | None
    message_ids: tuple[str, ...]
    first_message_id: str
    last_message_id: str
    payload_digest: str


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _thread_id(record: dict[str, Any]) -> str | None:
    # DiscordChatExporter identifies a forum thread in channel.id. Normal
    # channels have no thread_name and therefore no separate thread identity.
    return str(record["channel_id"]) if record.get("thread_name") else None


def build_message_index(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        message_id = str(record["id"])
        if message_id in index and index[message_id] != record:
            raise OwnershipError(f"conflicting baseline records for message {message_id}")
        index[message_id] = record
    return index


def resolve_root(message_id: str, records: dict[str, dict[str, Any]]) -> str:
    """Resolve the oldest known reply ancestor, failing on cycles."""
    current = message_id
    seen: set[str] = set()
    while True:
        if current in seen:
            raise OwnershipError(f"reply cycle detected at message {current}")
        seen.add(current)
        record = records.get(current)
        if not record or not record.get("parent_id"):
            return current
        parent = str(record["parent_id"])
        if parent not in records:
            return parent
        current = parent


def _validate_payload(point_id: str, payload: dict[str, Any]) -> tuple[list[str], str]:
    required = ("channel_id", "message_ids", "first_message_id", "split_index")
    missing = [key for key in required if payload.get(key) in (None, "")]
    if missing:
        raise OwnershipError(
            f"point {point_id} missing required payload fields: {', '.join(missing)}"
        )
    message_ids = [str(value) for value in payload["message_ids"]]
    if not message_ids or len(message_ids) != len(set(message_ids)):
        raise OwnershipError(f"point {point_id} has empty or duplicate message_ids")
    if str(payload["first_message_id"]) != message_ids[0]:
        raise OwnershipError(f"point {point_id} first_message_id is inconsistent")
    expected = str(
        _stable_id(
            {
                "message_ids": message_ids,
                "split_index": int(payload["split_index"]),
            }
        )
    )
    if expected != str(point_id):
        raise OwnershipError(
            f"point {point_id} does not match deterministic stable ID {expected}"
        )
    return message_ids, str(payload["channel_id"])


def point_to_manifest(
    point_id: str,
    payload: dict[str, Any],
    records: dict[str, dict[str, Any]] | None = None,
) -> ManifestRow:
    message_ids, channel_id = _validate_payload(point_id, payload)
    # Qdrant is authoritative for the historical baseline. Old chunker versions
    # could include a cross-channel reply root in a chunk, so export records are
    # optional enrichment and must not redefine the point's owning scope.
    thread_name = payload.get("thread_name")
    thread_id = (
        str(payload["thread_id"])
        if payload.get("thread_id") is not None
        else channel_id if thread_name else None
    )
    root_message_id: str | None = None
    logical_group_id = f"point:{channel_id}:{thread_id or '-'}:{point_id}"
    if records and all(message_id in records for message_id in message_ids):
        point_records = [records[message_id] for message_id in message_ids]
        reply_ids = [
            message_id for message_id, record in zip(message_ids, point_records)
            if record.get("parent_id")
        ]
        roots = {resolve_root(message_id, records) for message_id in reply_ids}
        candidate_root = next(iter(roots)) if len(roots) == 1 else None
        root_record = records.get(candidate_root) if candidate_root else None
        ownership_records = point_records + ([root_record] if root_record else [])
        scope_is_safe = bool(root_record) and all(
            str(record["channel_id"]) == channel_id
            and record.get("thread_name") == thread_name
            for record in ownership_records
        )
        # Split pieces can omit their root. Infer it only when every piece
        # message resolves to one available root and all records agree with
        # Qdrant's authoritative channel/thread scope.
        is_reply_group = (
            scope_is_safe
            and all(
                message_id == candidate_root
                or resolve_root(message_id, records) == candidate_root
                for message_id in message_ids
            )
        )
        if is_reply_group:
            root_message_id = candidate_root
            logical_group_id = (
                f"reply:{channel_id}:{thread_id or '-'}:"
                f"{root_message_id}"
            )

    owned_payload = {
        "channel_id": channel_id,
        "thread_id": thread_id,
        "thread_name": thread_name,
        "message_ids": message_ids,
        "first_message_id": str(payload["first_message_id"]),
        "split_index": int(payload["split_index"]),
    }
    return ManifestRow(
        point_id=str(point_id),
        logical_group_id=logical_group_id,
        channel_id=channel_id,
        thread_id=thread_id,
        root_message_id=root_message_id,
        message_ids=tuple(message_ids),
        first_message_id=message_ids[0],
        last_message_id=message_ids[-1],
        payload_digest=_canonical_digest(owned_payload),
    )


def scan_qdrant(client: Any, collection: str) -> list[tuple[str, dict[str, Any]]]:
    points: list[tuple[str, dict[str, Any]]] = []
    offset = None
    while True:
        page, offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in page:
            points.append((str(point.id), point.payload or {}))
        if offset is None:
            break
    return points


def create_plan(
    points: Iterable[tuple[str, dict[str, Any]]],
    records: Iterable[dict[str, Any]] | None,
    collection: str,
    chunker_version: str,
    embedding_version: str,
) -> dict[str, Any]:
    record_index = build_message_index(records or [])
    rows = [
        point_to_manifest(point_id, payload, record_index or None)
        for point_id, payload in points
    ]
    rows.sort(key=lambda row: int(row.point_id))
    if len(rows) != len({row.point_id for row in rows}):
        raise OwnershipError("Qdrant scan returned duplicate point IDs")
    serial_rows = [
        {**asdict(row), "message_ids": list(row.message_ids)} for row in rows
    ]
    digest = _canonical_digest(serial_rows)
    identity_digest = _canonical_digest(
        {
            "collection_name": collection,
            "manifest_digest": digest,
            "chunker_version": chunker_version,
            "embedding_version": embedding_version,
        }
    )
    return {
        "plan_version": PLAN_VERSION,
        "collection_name": collection,
        "chunker_version": chunker_version,
        "embedding_version": embedding_version,
        "point_count": len(rows),
        "manifest_digest": digest,
        "run_id": f"baseline-{identity_digest[:20]}",
        "rows": serial_rows,
    }


def verify_plan(
    plan: dict[str, Any], points: Iterable[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """Verify a saved plan against a fresh, read-only Qdrant scan."""
    rows = plan.get("rows")
    if not isinstance(rows, list):
        raise OwnershipError("saved plan has no rows list")
    canonical_rows = sorted(rows, key=lambda row: int(row["point_id"]))
    recomputed_digest = _canonical_digest(canonical_rows)
    if plan.get("point_count") != len(canonical_rows):
        raise OwnershipError(
            f"saved plan point_count {plan.get('point_count')} != "
            f"row count {len(canonical_rows)}"
        )
    if plan.get("manifest_digest") != recomputed_digest:
        raise OwnershipError("saved plan manifest_digest does not match its rows")
    planned = {row["point_id"]: row for row in canonical_rows}
    if len(planned) != len(canonical_rows):
        raise OwnershipError("saved plan contains duplicate point IDs")
    live: dict[str, str] = {}
    for point_id, payload in points:
        if point_id in live:
            raise OwnershipError(f"Qdrant scan returned duplicate point {point_id}")
        row = point_to_manifest(point_id, payload)
        live[point_id] = row.payload_digest
    missing = sorted(set(planned) - set(live), key=int)
    unexpected = sorted(set(live) - set(planned), key=int)
    mismatched = sorted(
        (
            point_id for point_id in set(planned) & set(live)
            if planned[point_id]["payload_digest"] != live[point_id]
        ),
        key=int,
    )
    if missing or unexpected or mismatched:
        raise OwnershipError(
            "manifest verification failed: "
            f"missing={len(missing)}, unexpected={len(unexpected)}, "
            f"payload_mismatch={len(mismatched)}"
        )
    return {
        "verified": True,
        "point_count": len(live),
        "manifest_digest": plan["manifest_digest"],
    }


def apply_plan(connection: Any, plan: dict[str, Any]) -> None:
    """Atomically replace only baseline-seed manifest state in Postgres."""
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO rag_ingestion_runs
                    (run_id, run_kind, status, collection_name, chunker_version,
                     embedding_version, point_count, manifest_digest, completed_at)
                VALUES (%s, 'baseline_seed', 'completed', %s, %s, %s, %s, %s, now())
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    point_count = EXCLUDED.point_count,
                    manifest_digest = EXCLUDED.manifest_digest,
                    completed_at = now()
                """,
                (
                    plan["run_id"],
                    plan["collection_name"],
                    plan["chunker_version"],
                    plan["embedding_version"],
                    plan["point_count"],
                    plan["manifest_digest"],
                ),
            )
            cursor.execute(
                """
                CREATE TEMP TABLE rag_chunk_manifest_stage
                    (LIKE rag_chunk_manifest INCLUDING DEFAULTS)
                ON COMMIT DROP
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE rag_chunk_ownership_stage
                    (LIKE rag_chunk_message_ownership INCLUDING DEFAULTS)
                ON COMMIT DROP
                """
            )
            with cursor.copy(
                """
                COPY rag_chunk_manifest_stage
                    (point_id, collection_name, logical_group_id, channel_id,
                     thread_id, root_message_id, message_ids, first_message_id,
                     last_message_id, chunker_version, embedding_version,
                     ingestion_run_id, payload_digest, active)
                FROM STDIN
                """
            ) as manifest_copy:
                for row in plan["rows"]:
                    manifest_copy.write_row((
                        row["point_id"], plan["collection_name"],
                        row["logical_group_id"], row["channel_id"],
                        row["thread_id"], row["root_message_id"],
                        row["message_ids"], row["first_message_id"],
                        row["last_message_id"], plan["chunker_version"],
                        plan["embedding_version"], plan["run_id"],
                        row["payload_digest"], True,
                    ))
            with cursor.copy(
                """
                COPY rag_chunk_ownership_stage
                    (point_id, message_id, message_position)
                FROM STDIN
                """
            ) as ownership_copy:
                for row in plan["rows"]:
                    for position, message_id in enumerate(row["message_ids"]):
                        ownership_copy.write_row(
                            (row["point_id"], message_id, position)
                        )
            cursor.execute(
                """
                DELETE FROM rag_chunk_message_ownership ownership
                USING rag_chunk_manifest manifest
                WHERE ownership.point_id = manifest.point_id
                  AND manifest.collection_name = %s
                """,
                (plan["collection_name"],),
            )
            cursor.execute(
                """
                INSERT INTO rag_chunk_manifest
                    (point_id, collection_name, logical_group_id, channel_id, thread_id,
                     root_message_id, message_ids, first_message_id,
                     last_message_id, chunker_version, embedding_version,
                     ingestion_run_id, payload_digest, active)
                SELECT point_id, collection_name, logical_group_id, channel_id,
                       thread_id, root_message_id, message_ids, first_message_id,
                       last_message_id, chunker_version, embedding_version,
                       ingestion_run_id, payload_digest, active
                FROM rag_chunk_manifest_stage
                ON CONFLICT (point_id) DO UPDATE SET
                    collection_name=EXCLUDED.collection_name,
                    logical_group_id=EXCLUDED.logical_group_id,
                    channel_id=EXCLUDED.channel_id,
                    thread_id=EXCLUDED.thread_id,
                    root_message_id=EXCLUDED.root_message_id,
                    message_ids=EXCLUDED.message_ids,
                    first_message_id=EXCLUDED.first_message_id,
                    last_message_id=EXCLUDED.last_message_id,
                    chunker_version=EXCLUDED.chunker_version,
                    embedding_version=EXCLUDED.embedding_version,
                    ingestion_run_id=EXCLUDED.ingestion_run_id,
                    payload_digest=EXCLUDED.payload_digest,
                    active=true, superseded_at=NULL
                """
            )
            cursor.execute(
                """
                INSERT INTO rag_chunk_message_ownership
                    (point_id, message_id, message_position)
                SELECT point_id, message_id, message_position
                FROM rag_chunk_ownership_stage
                """
            )
            cursor.execute(
                """
                UPDATE rag_chunk_manifest
                SET active=false, superseded_at=now()
                WHERE active AND collection_name=%s AND ingestion_run_id <> %s
                """,
                (plan["collection_name"], plan["run_id"]),
            )
            cursor.execute(
                """
                UPDATE rag_corpus_versions
                SET status='superseded', superseded_at=now()
                WHERE collection_name=%s
                  AND status='healthy'
                  AND corpus_version_id <> %s
                """,
                (plan["collection_name"], plan["run_id"]),
            )
            cursor.execute(
                """
                INSERT INTO rag_corpus_versions
                    (corpus_version_id, ingestion_run_id, collection_name,
                     manifest_digest, point_count, status, activated_at)
                VALUES (%s,%s,%s,%s,%s,'healthy',now())
                ON CONFLICT (corpus_version_id) DO UPDATE SET
                    ingestion_run_id=EXCLUDED.ingestion_run_id,
                    collection_name=EXCLUDED.collection_name,
                    manifest_digest=EXCLUDED.manifest_digest,
                    point_count=EXCLUDED.point_count,
                    status='healthy',
                    activated_at=now(),
                    superseded_at=NULL
                """,
                (
                    plan["run_id"], plan["run_id"], plan["collection_name"],
                    plan["manifest_digest"], plan["point_count"],
                ),
            )
            cursor.execute(
                """
                SELECT count(*) FROM rag_chunk_manifest
                WHERE active AND collection_name=%s
                """,
                (plan["collection_name"],),
            )
            active_count = cursor.fetchone()[0]
            if active_count != plan["point_count"]:
                raise OwnershipError(
                    f"post-apply manifest count {active_count} != "
                    f"planned {plan['point_count']}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument(
        "--exports",
        help="optional DiscordChatExporter directory for safe reply-root enrichment",
    )
    parser.add_argument("--chunker-version", default="v10")
    parser.add_argument("--embedding-version", default="nomic-embed-text-v1.5")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-plan", type=Path)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()

    from qdrant_client import QdrantClient

    client = QdrantClient(url=args.qdrant_url)
    points = scan_qdrant(client, args.collection)
    if args.verify_plan:
        saved = json.loads(args.verify_plan.read_text(encoding="utf-8"))
        print(json.dumps(verify_plan(saved, points), indent=2, sort_keys=True))
        return 0
    records = parse_all_exports(args.exports) if args.exports else None
    plan = create_plan(
        points,
        records,
        args.collection,
        args.chunker_version,
        args.embedding_version,
    )
    rendered = json.dumps(plan, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)

    if args.apply:
        if not args.database_url:
            parser.error("--apply requires --database-url or DATABASE_URL")
        import psycopg
        with psycopg.connect(args.database_url) as connection:
            apply_plan(connection, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
