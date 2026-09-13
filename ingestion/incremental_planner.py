"""Phase 9C.3 deterministic offline planner and shadow rechunker.

The planner reads captured messages, the ownership manifest, and Qdrant payloads.
It never calls a Qdrant mutation API. Plan persistence is opt-in and writes only
the Phase 9C.3 Postgres plan tables for later Phase 9C.4 execution.

Captured Discord messages are treated as immutable MESSAGE_CREATE records.
Discord message edits and deletions are deliberately outside the incremental
ingestion contract; an operator may use the full rebuild path when historical
corpus correction is important enough to warrant it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ingestion.chunk_manifest import scan_qdrant
from ingestion.chunker import OVERLAP_MSGS, WINDOW_MINS, chunk_records
from ingestion.parser import parse_all_exports
from ingestion.reply_roots import resolve_reply_root
from ingestion.run import _stable_id

PLAN_VERSION = 3
DEFAULT_COLLECTION = "tpm_unite_history"


class PlanningError(ValueError):
    """The pending batch cannot be planned deterministically."""


@dataclass(frozen=True)
class WorkItem:
    message_id: str
    capture_sequence: int
    work_kind: str
    channel_id: str
    thread_id: str | None
    parent_message_id: str | None


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _scope(record: dict[str, Any]) -> str:
    """Return the sole chunk-membership boundary: Discord channel_id."""
    return str(record["channel_id"])


def _record_index(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        message_id = str(record["id"])
        # Live capture is appended after exports and therefore wins.
        result[message_id] = {**record, "id": message_id}
    return result


def coalesce_work(
    work: Iterable[WorkItem],
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Coalesce replies by trusted root and channel-local fallback windows."""
    index = _record_index(records)
    reply_groups: dict[tuple[str, str], list[WorkItem]] = {}
    windows: dict[str, list[WorkItem]] = {}
    ordered_work = sorted(work, key=lambda value: value.capture_sequence)
    recent_ids = {
        item.message_id for item in ordered_work
        if item.work_kind == "recent_window"
    }
    repaired_roots: dict[str, set[str]] = {message_id: set() for message_id in recent_ids}
    for message_id, record in index.items():
        if not record.get("parent_id"):
            continue
        resolution = resolve_reply_root(message_id, index, record["channel_id"])
        if resolution.root_message_id is None:
            continue
        for ancestor_id in _ancestor_ids(message_id, index).intersection(recent_ids):
            repaired_roots[ancestor_id].add(resolution.root_message_id)
    for item in ordered_work:
        if item.message_id not in index:
            raise PlanningError(f"pending message {item.message_id} is missing")
        record_channel_id = _scope(index[item.message_id])
        if item.channel_id != record_channel_id:
            raise PlanningError(
                f"pending message {item.message_id} channel_id {item.channel_id} "
                f"does not match source record {record_channel_id}"
            )
        if item.work_kind == "reply_conversation":
            root = resolve_reply_root(
                item.message_id, index, item.channel_id
            ).root_message_id
            if root is None:
                windows.setdefault(item.channel_id, []).append(item)
            else:
                reply_groups.setdefault((item.channel_id, root), []).append(item)
        elif item.work_kind != "recent_window":
            raise PlanningError(f"unsupported work kind {item.work_kind}")

    # A root captured earlier in this same batch starts as recent_window work.
    # If a reply to it is also present, absorb that root into the proven reply
    # conversation instead of producing a duplicate window plan.
    for item in ordered_work:
        if item.work_kind != "recent_window":
            continue
        newly_resolved_roots = repaired_roots[item.message_id]
        if len(newly_resolved_roots) == 1:
            root = next(iter(newly_resolved_roots))
            reply_groups.setdefault((item.channel_id, root), []).append(item)
            continue
        matching_reply_key = next(
            (
                key for key in reply_groups
                if key[0] == item.channel_id
                and key[1] == item.message_id
            ),
            None,
        )
        if matching_reply_key:
            reply_groups[matching_reply_key].append(item)
        else:
            windows.setdefault(item.channel_id, []).append(item)

    groups: list[dict[str, Any]] = []
    for (channel_id, root), items in reply_groups.items():
        groups.append({
            "group_key": f"reply:{channel_id}:{root}",
            "work_kind": "reply_conversation",
            "channel_id": channel_id,
            "thread_id": items[0].thread_id,
            "root_message_id": root,
            "source_message_ids": sorted(
                (item.message_id for item in items), key=int
            ),
        })
    threshold = timedelta(minutes=WINDOW_MINS)
    for channel_id, items in windows.items():
        ordered = sorted(items, key=lambda item: _dt(index[item.message_id]["timestamp"]))
        batches: list[list[WorkItem]] = []
        batch_start: datetime | None = None
        for item in ordered:
            if not batches:
                batches.append([item])
                batch_start = _dt(index[item.message_id]["timestamp"])
                continue
            current = _dt(index[item.message_id]["timestamp"])
            # chunker._window_chunk only closes a >15m window after it has
            # MIN_MSGS messages. A singleton therefore waits for the next
            # same-scope message even when that message arrives much later.
            if current - batch_start > threshold and len(batches[-1]) >= 2:
                batches.append([item])
                batch_start = current
                continue
            batches[-1].append(item)
        for batch in batches:
            source_ids = [item.message_id for item in batch]
            start = _dt(index[source_ids[0]]["timestamp"])
            key = f"window:{channel_id}:{start.isoformat()}"
            groups.append({
                "group_key": key,
                "work_kind": "recent_window",
                "channel_id": channel_id,
                "thread_id": batch[0].thread_id,
                "root_message_id": None,
                "source_message_ids": source_ids,
            })
    return sorted(groups, key=lambda group: group["group_key"])


def _ancestor_ids(
    message_id: str, records: dict[str, dict[str, Any]]
) -> set[str]:
    """Return available ancestors for late-ancestor repair detection."""
    ancestors: set[str] = set()
    current = str(message_id)
    while current in records and current not in ancestors:
        ancestors.add(current)
        parent = records[current].get("parent_id")
        if not parent:
            break
        current = str(parent)
    return ancestors


def _point_payloads(
    points: Iterable[tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for point_id, payload in points:
        if point_id in result:
            raise PlanningError(f"duplicate Qdrant point {point_id}")
        result[str(point_id)] = payload
    return result


def _chunk_row(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "point_id": str(_stable_id(chunk)),
        "message_ids": [str(value) for value in chunk["message_ids"]],
        "first_message_id": str(chunk["first_message_id"]),
        "split_index": int(chunk.get("split_index", 0)),
        "channel_id": str(chunk["channel_id"]),
        "thread_name": chunk.get("thread_name"),
        "root_message_id": chunk.get("root_message_id"),
        "text_digest": hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest(),
        "text": chunk["text"],
        # Private apply material. It is deliberately excluded from the
        # persisted plan identity below so existing Phase 9C.3 plans remain
        # byte-for-byte deterministic. Phase 9C.4 reconstructs it from source
        # records and verifies the persisted identity before any mutation.
        "_payload": {
            "text": chunk["text"],
            "start_ts": chunk["start_ts"],
            "end_ts": chunk["end_ts"],
            "channel": chunk["channel"],
            "channel_id": chunk.get("channel_id"),
            "thread_name": chunk.get("thread_name"),
            "authors": chunk["authors"],
            "message_count": chunk["message_count"],
            "message_ids": [str(value) for value in chunk.get("message_ids", [])],
            "first_message_id": str(chunk.get("first_message_id", "")),
            "root_message_id": chunk.get("root_message_id"),
            "token_count": chunk.get("token_count", 0),
            "span_days": chunk.get("span_days", 0),
            "split_index": int(chunk.get("split_index", 0)),
        },
    }


def _shadow_records(
    group: dict[str, Any],
    index: dict[str, dict[str, Any]],
    point_payloads: dict[str, dict[str, Any]],
    manifest: list[dict[str, Any]],
    root_members: dict[tuple[str, str], set[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    scope = group["channel_id"]
    source = [index[message_id] for message_id in group["source_message_ids"]]
    old_ids: set[str] = set()
    selected_ids: set[str] = set(group["source_message_ids"])

    if group["work_kind"] == "reply_conversation":
        root = group["root_message_id"]
        for row in manifest:
            if (
                row.get("active", True)
                and str(row["channel_id"]) == scope
                and (
                    row.get("root_message_id") == root
                    or root in {str(value) for value in row["message_ids"]}
                )
            ):
                old_ids.add(str(row["point_id"]))
                selected_ids.update(str(value) for value in row["message_ids"])
        selected_ids.update(root_members.get((scope, root), set()))
        # A late ancestor can turn previously unresolved window chunks into a
        # trusted reply conversation. Replace every same-scope point that owns
        # one of those descendants, even though its old root was null.
        changed = True
        while changed:
            changed = False
            for row in manifest:
                row_message_ids = {
                    str(value) for value in row.get("message_ids", [])
                }
                point_id = str(row["point_id"])
                if (
                    row.get("active", True)
                    and point_id not in old_ids
                    and str(row["channel_id"]) == scope
                    and row_message_ids.intersection(selected_ids)
                ):
                    old_ids.add(point_id)
                    selected_ids.update(row_message_ids)
                    changed = True
    else:
        earliest = min(_dt(record["timestamp"]) for record in source)
        latest = max(_dt(record["timestamp"]) for record in source)
        lower = earliest - timedelta(minutes=WINDOW_MINS)
        upper = latest + timedelta(minutes=WINDOW_MINS)
        candidates: list[tuple[datetime, str]] = []
        for message_id, record in index.items():
            if _scope(record) == scope:
                timestamp = _dt(record["timestamp"])
                if lower <= timestamp <= upper:
                    candidates.append((timestamp, message_id))
        candidates.sort()
        selected_ids.update(message_id for _, message_id in candidates)
        for point_id, payload in point_payloads.items():
            payload_scope = str(payload.get("channel_id"))
            if payload_scope != scope or not payload.get("start_ts"):
                continue
            if _dt(payload["end_ts"]) >= lower and _dt(payload["start_ts"]) <= upper:
                old_ids.add(point_id)
                selected_ids.update(str(value) for value in payload.get("message_ids", []))
        # Include configured overlap only while records remain in the same
        # 15-minute window. A months-old neighbor must never make a new isolated
        # message appear chunkable.
        scope_records = sorted(
            (
                (_dt(record["timestamp"]), message_id)
                for message_id, record in index.items()
                if _scope(record) == scope
            )
        )
        positions = [i for i, (_, mid) in enumerate(scope_records) if mid in selected_ids]
        if positions:
            lo = min(positions)
            hi = max(positions)
            for _ in range(OVERLAP_MSGS):
                if lo > 0 and scope_records[lo][0] - scope_records[lo - 1][0] <= timedelta(minutes=WINDOW_MINS):
                    lo -= 1
                if hi + 1 < len(scope_records) and scope_records[hi + 1][0] - scope_records[hi][0] <= timedelta(minutes=WINDOW_MINS):
                    hi += 1
            selected_ids.update(mid for _, mid in scope_records[lo:hi + 1])

    records = [
        index[message_id] for message_id in selected_ids if message_id in index
    ]
    records.sort(key=lambda record: (_dt(record["timestamp"]), str(record["id"])))
    return records, sorted(old_ids, key=int)


def create_shadow_plan(
    work: Iterable[WorkItem],
    records: Iterable[dict[str, Any]],
    manifest: Iterable[dict[str, Any]],
    points: Iterable[tuple[str, dict[str, Any]]],
    collection: str = DEFAULT_COLLECTION,
    chunker_version: str = "v11",
    embedding_version: str = "nomic-ai/nomic-embed-text-v1.5",
    source_corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work_list = list(work)
    records_list = list(records)
    index = _record_index(records_list)
    manifest_list = list(manifest)
    payloads = _point_payloads(points)
    groups = coalesce_work(work_list, records_list)
    root_members: dict[tuple[str, str], set[str]] = {}
    for message_id, record in index.items():
        resolution = resolve_reply_root(message_id, index, record["channel_id"])
        if resolution.root_message_id is not None:
            root_members.setdefault(
                (str(record["channel_id"]), resolution.root_message_id), set()
            ).add(message_id)
    planned_groups: list[dict[str, Any]] = []
    for group in groups:
        selected, old_ids = _shadow_records(
            group, index, payloads, manifest_list, root_members
        )
        chunks = chunk_records(selected)
        replacements = sorted((_chunk_row(chunk) for chunk in chunks), key=lambda row: int(row["point_id"]))
        if group["work_kind"] == "recent_window":
            source_ids = set(group["source_message_ids"])
            old_point_ids = set(old_ids)
            # Bounded reconstruction can start inside an earlier overlap chain.
            # Keep unchanged old pieces and pieces containing pending messages;
            # discard a new prefix that only duplicates an unaffected neighbor.
            replacements = [
                row for row in replacements
                if row["point_id"] in old_point_ids
                or source_ids.intersection(row["message_ids"])
            ]
        status = "ready" if replacements else "deferred"
        planned_groups.append({
            **group,
            "status": status,
            "old_point_ids": old_ids,
            "replacement_points": replacements,
            "selected_message_count": len(selected),
        })
    cutoff = max((item.capture_sequence for item in work_list), default=0)
    public_groups = []
    for value in planned_groups:
        group = dict(value)
        replacement_rows = group.pop("replacement_points")
        group["replacement_point_ids"] = [
            row["point_id"] for row in replacement_rows
        ]
        # Text stays out of the persisted plan, but its digest and all ownership
        # metadata participate in the immutable plan identity.
        group["replacement_points"] = [
            {
                key: item
                for key, item in row.items()
                if key != "text" and not key.startswith("_")
            }
            for row in replacement_rows
        ]
        public_groups.append(group)
    identity = {
        "plan_version": PLAN_VERSION,
        "collection_name": collection,
        "batch_cutoff_sequence": cutoff,
        "chunker_version": chunker_version,
        "embedding_version": embedding_version,
        "source_corpus_version_id": (
            source_corpus.get("corpus_version_id") if source_corpus else None
        ),
        "source_manifest_digest": (
            source_corpus.get("manifest_digest") if source_corpus else None
        ),
        "source_active_revision": (
            source_corpus.get("active_revision") if source_corpus else None
        ),
        "source_logical_name": (
            source_corpus.get("logical_name") if source_corpus else None
        ),
        "source_chunker_version": (
            source_corpus.get("chunker_version") if source_corpus else None
        ),
        "groups": public_groups,
    }
    digest = _digest(identity)
    return {
        **identity,
        "plan_id": f"shadow-{digest[:20]}",
        "plan_digest": digest,
        "pending_message_count": sum(len(g["source_message_ids"]) for g in public_groups),
        "old_point_count": len({p for g in public_groups for p in g["old_point_ids"]}),
        "replacement_point_count": len({
            p for g in public_groups for p in g["replacement_point_ids"]
        }),
        "_replacement_details": {
            group["group_key"]: rows
            for group, rows in zip(public_groups, (
                value["replacement_points"] for value in planned_groups
            ))
        },
    }


def _measure_embeddings(texts: Iterable[str], embedder_url: str, kind: str) -> dict[str, Any]:
    started = time.perf_counter()
    count = 0
    dimensions: set[int] = set()
    models: set[str] = set()
    for text in texts:
        request = urllib.request.Request(
            embedder_url.rstrip("/") + "/embed",
            data=json.dumps({"text": "search_document: " + text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
        dimensions.add(int(result["dimension"]))
        if result.get("model"):
            models.add(str(result["model"]))
        count += 1
    elapsed = time.perf_counter() - started
    return {
        "measurement_kind": kind,
        "embedded_chunk_count": count,
        "embedding_dimensions": sorted(dimensions),
        "observed_embedding_models": sorted(models),
        "measured_embedding_seconds": round(elapsed, 3),
        "chunks_per_minute": round(count / elapsed * 60, 2) if elapsed else None,
    }


def embed_shadow(plan: dict[str, Any], embedder_url: str) -> dict[str, Any]:
    """Embed replacement text for timing/dimension proof; never writes Qdrant."""
    unique: dict[str, dict[str, Any]] = {}
    for rows in plan["_replacement_details"].values():
        for row in rows:
            point_id = str(row["point_id"])
            existing = unique.get(point_id)
            if existing is not None and existing != row:
                raise PlanningError(f"conflicting replacement point {point_id}")
            unique[point_id] = row
    texts = [unique[point_id]["text"] for point_id in sorted(unique, key=int)]
    return _measure_embeddings(texts, embedder_url, "shadow_replacements")


def benchmark_embedder(
    points: Iterable[tuple[str, dict[str, Any]]],
    embedder_url: str,
    sample_size: int = 10,
) -> dict[str, Any]:
    """Measure Railway with a deterministic existing-chunk sample."""
    sample = [
        payload["text"]
        for _, payload in sorted(points, key=lambda value: int(value[0]))
        if payload.get("text")
    ][:sample_size]
    return _measure_embeddings(sample, embedder_url, "existing_point_sample")


def render_plan(
    plan: dict[str, Any],
    measurement: dict[str, Any] | None = None,
    *,
    source_corpus_current: bool | None = None,
) -> dict[str, Any]:
    rendered = {key: value for key, value in plan.items() if not key.startswith("_")}
    rendered["measurement"] = measurement
    rendered["qdrant_mutations"] = 0
    rendered["ready_group_count"] = sum(g["status"] == "ready" for g in rendered["groups"])
    rendered["deferred_group_count"] = sum(g["status"] == "deferred" for g in rendered["groups"])
    embedded_count = (measurement or {}).get("embedded_chunk_count")
    dimensions = (measurement or {}).get("embedding_dimensions")
    observed_models = (measurement or {}).get("observed_embedding_models")
    measurement_kind = (measurement or {}).get("measurement_kind")
    embedding_complete = (
        rendered["replacement_point_count"] == 0
        or (
            measurement_kind == "shadow_replacements"
            and embedded_count == rendered["replacement_point_count"]
            and dimensions == [768]
            and observed_models == [rendered["embedding_version"]]
        )
    )
    source_linked = bool(
        rendered.get("source_corpus_version_id")
        and rendered.get("source_manifest_digest")
    )
    source_version_matches = (
        rendered.get("source_chunker_version") == rendered["chunker_version"]
    )
    checks = {
        "complete_production_embeddings": embedding_complete,
        "zero_qdrant_mutations": rendered["qdrant_mutations"] == 0,
        "source_corpus_linked": source_linked,
        "source_corpus_current": source_corpus_current is True,
        "source_chunker_version_matches": source_version_matches,
    }
    contradictions: list[str] = []
    missing: list[str] = []
    if measurement is None:
        if rendered["replacement_point_count"]:
            missing.append("production replacement embedding evidence")
    elif rendered["replacement_point_count"]:
        if embedded_count != rendered["replacement_point_count"]:
            contradictions.append("embedded count does not equal replacement count")
        if dimensions != [768]:
            contradictions.append("embedding dimensions are not exactly 768")
        if observed_models != [rendered["embedding_version"]]:
            contradictions.append("observed embedding model/version mismatch")
        if measurement_kind != "shadow_replacements":
            contradictions.append("measurement is not replacement embedding evidence")
    if not source_linked:
        missing.append("source corpus linkage")
    if rendered.get("source_chunker_version") is None:
        missing.append("source corpus chunker version")
    elif not source_version_matches:
        contradictions.append("source corpus chunker version mismatch")
    if source_corpus_current is None:
        missing.append("source corpus freshness evidence")
    elif source_corpus_current is not True:
        contradictions.append("source corpus is stale")
    if rendered["qdrant_mutations"] != 0:
        contradictions.append("Qdrant mutation evidence is non-zero")

    if contradictions:
        status = "failed"
    elif not rendered["ready_group_count"]:
        status = "deferred"
    elif all(checks.values()):
        status = "shadow_validated"
    else:
        status = "planned"
    rendered["validation"] = {
        "status": status,
        "checks": checks,
        "missing_evidence": missing,
        "contradictions": contradictions,
    }
    return rendered


def _validate_status_transition(existing: str, requested: str) -> None:
    allowed = {
        "planned": {"planned", "shadow_validated", "deferred", "failed"},
        "shadow_validated": {"shadow_validated", "failed"},
        "deferred": {"deferred", "failed"},
        "failed": {"failed"},
    }
    if requested not in allowed.get(existing, set()):
        raise PlanningError(
            f"invalid persisted plan transition {existing} -> {requested}"
        )


def _validate_existing_groups(
    plan_id: str,
    existing_groups: Iterable[tuple[Any, ...]],
    expected_groups: Iterable[dict[str, Any]],
) -> None:
    rows = list(existing_groups)
    groups = sorted(expected_groups, key=lambda group: group["group_key"])
    if len(rows) != len(groups):
        raise PlanningError(f"existing plan {plan_id} has a different group set")
    for row, group in zip(rows, groups):
        group_identity = (
            group["group_key"],
            group["work_kind"],
            group["channel_id"],
            group["thread_id"],
            group["root_message_id"],
            list(group["source_message_ids"]),
            list(group["old_point_ids"]),
            list(group["replacement_point_ids"]),
            group["status"],
        )
        if tuple(row[:-1]) != group_identity or _digest(row[-1]) != _digest(group):
            raise PlanningError(
                f"existing plan {plan_id} group "
                f"{group['group_key']} differs from immutable evidence"
            )


def persist_plan(connection: Any, rendered: dict[str, Any]) -> None:
    """Persist immutable plan evidence without claiming or completing work."""
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cv.corpus_version_id, cv.manifest_digest,
                       run.chunker_version
                FROM rag_corpus_versions cv
                JOIN rag_ingestion_runs run
                  ON run.run_id = cv.ingestion_run_id
                WHERE cv.collection_name=%s AND cv.status='healthy'
                FOR SHARE
                """,
                (rendered["collection_name"],),
            )
            current_corpus = cursor.fetchone()
            current_matches = bool(
                current_corpus
                and current_corpus[0] == rendered.get("source_corpus_version_id")
                and current_corpus[1] == rendered.get("source_manifest_digest")
                and current_corpus[2] == rendered.get("source_chunker_version")
                and current_corpus[2] == rendered.get("chunker_version")
            )
            validation = dict(rendered.get("validation") or {})
            contradictions = list(validation.get("contradictions") or [])
            if not current_matches:
                contradictions.append("source corpus changed before persistence")
                validation["status"] = "failed"
                validation["contradictions"] = contradictions
                checks = dict(validation.get("checks") or {})
                checks["source_corpus_current"] = False
                validation["checks"] = checks
                rendered["validation"] = validation
            status = validation.get("status", "planned")
            measurement = rendered.get("measurement") or {}
            estimated = measurement.get("measured_embedding_seconds")

            cursor.execute(
                """
                SELECT collection_name, batch_cutoff_sequence, chunker_version,
                       embedding_version, source_corpus_version_id,
                       source_manifest_digest, plan_digest, old_point_count,
                       replacement_point_count, pending_message_count, status
                FROM rag_chunk_replacement_plans
                WHERE plan_id=%s
                FOR UPDATE
                """,
                (rendered["plan_id"],),
            )
            existing = cursor.fetchone()
            immutable = (
                rendered["collection_name"],
                rendered["batch_cutoff_sequence"],
                rendered["chunker_version"],
                rendered["embedding_version"],
                rendered.get("source_corpus_version_id"),
                rendered.get("source_manifest_digest"),
                rendered["plan_digest"],
                rendered["old_point_count"],
                rendered["replacement_point_count"],
                rendered["pending_message_count"],
            )
            if existing is not None and tuple(existing[:-1]) != immutable:
                raise PlanningError(
                    f"existing plan {rendered['plan_id']} has different immutable identity"
                )
            if existing is not None:
                _validate_status_transition(str(existing[-1]), status)
                cursor.execute(
                    """
                    SELECT group_key, work_kind, channel_id, thread_id,
                           root_message_id, source_message_ids, old_point_ids,
                           replacement_point_ids, status, evidence
                    FROM rag_chunk_replacement_plan_groups
                    WHERE plan_id=%s
                    ORDER BY group_key
                    """,
                    (rendered["plan_id"],),
                )
                existing_groups = cursor.fetchall()
                _validate_existing_groups(
                    rendered["plan_id"], existing_groups, rendered["groups"]
                )
            cursor.execute(
                """
                INSERT INTO rag_chunk_replacement_plans
                    (plan_id, collection_name, batch_cutoff_sequence,
                     chunker_version, embedding_version, source_corpus_version_id,
                     source_manifest_digest, status, plan_digest,
                     old_point_count, replacement_point_count,
                     pending_message_count, estimated_seconds,
                     measured_embedding_seconds, evidence, validated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                        CASE WHEN %s='shadow_validated' THEN now() ELSE NULL END)
                ON CONFLICT (plan_id) DO UPDATE SET
                    status=EXCLUDED.status,
                    estimated_seconds=EXCLUDED.estimated_seconds,
                    measured_embedding_seconds=EXCLUDED.measured_embedding_seconds,
                    evidence=EXCLUDED.evidence,
                    validated_at=CASE
                        WHEN EXCLUDED.status='shadow_validated' THEN now()
                        ELSE rag_chunk_replacement_plans.validated_at
                    END
                """,
                (
                    rendered["plan_id"], rendered["collection_name"],
                    rendered["batch_cutoff_sequence"], rendered["chunker_version"],
                    rendered["embedding_version"],
                    rendered.get("source_corpus_version_id"),
                    rendered.get("source_manifest_digest"),
                    status, rendered["plan_digest"],
                    rendered["old_point_count"], rendered["replacement_point_count"],
                    rendered["pending_message_count"], estimated, estimated,
                    json.dumps(rendered, sort_keys=True), status,
                ),
            )
            if existing is not None:
                return
            for group in rendered["groups"]:
                cursor.execute(
                    """
                    INSERT INTO rag_chunk_replacement_plan_groups
                        (plan_id, group_key, work_kind, channel_id, thread_id,
                         root_message_id, source_message_ids, old_point_ids,
                         replacement_point_ids, status, evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        rendered["plan_id"], group["group_key"], group["work_kind"],
                        group["channel_id"], group["thread_id"],
                        group["root_message_id"], group["source_message_ids"],
                        group["old_point_ids"], group["replacement_point_ids"],
                        group["status"], json.dumps(group, sort_keys=True),
                    ),
                )


def load_postgres(
    connection: Any,
    cutoff: int | None = None,
    collection: str = DEFAULT_COLLECTION,
    work_statuses: tuple[str, ...] = ("pending",),
) -> tuple[
    list[WorkItem],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    cutoff_sql = cutoff if cutoff is not None else 9223372036854775807
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT w.source_message_id, w.capture_sequence, w.work_kind,
                   m.channel_id, w.thread_id, w.parent_message_id
            FROM rag_pending_chunk_work w
            JOIN rag_discord_messages m ON m.message_id=w.source_message_id
            WHERE w.status=ANY(%s) AND w.capture_sequence <= %s
            ORDER BY w.capture_sequence
            """,
            (list(work_statuses), cutoff_sql),
        )
        work = [WorkItem(*row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT message_id, channel_id, channel_name, parent_channel_id,
                   parent_channel_name, thread_id, thread_name, parent_message_id,
                   author_display_name, content, message_created_at
            FROM rag_discord_messages
            WHERE capture_sequence <= %s
            ORDER BY message_created_at, message_id
            """,
            (cutoff_sql,),
        )
        live = [{
            "id": row[0], "channel_id": row[1], "channel": row[4],
            "thread_id": row[5], "thread_name": row[6], "parent_id": row[7],
            "author": row[8], "content": row[9], "timestamp": row[10].isoformat(),
        } for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT point_id, logical_group_id, channel_id, thread_id,
                   root_message_id, message_ids, active
            FROM rag_chunk_manifest WHERE active
            """
        )
        manifest = [{
            "point_id": row[0], "logical_group_id": row[1], "channel_id": row[2],
            "thread_id": row[3], "root_message_id": row[4],
            "message_ids": row[5], "active": row[6],
        } for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT cv.corpus_version_id, cv.manifest_digest,
                   run.chunker_version
            FROM rag_corpus_versions cv
            JOIN rag_ingestion_runs run ON run.run_id = cv.ingestion_run_id
            WHERE cv.collection_name=%s AND cv.status='healthy'
            """,
            (collection,),
        )
        corpus_row = cursor.fetchone()
        source_corpus = (
            {
                "corpus_version_id": corpus_row[0],
                "manifest_digest": corpus_row[1],
                "chunker_version": corpus_row[2],
            }
            if corpus_row
            else None
        )
    return work, live, manifest, source_corpus


def resolve_active_corpus(connection: Any, logical_name: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT collection_name,corpus_version_id,manifest_digest,capture_cutoff_sequence,revision,state "
        "FROM rag_active_corpus WHERE logical_name=%s OR control_collection_name=%s", (logical_name, logical_name)
    ).fetchone()
    if not row or row[5] != "serving":
        raise PlanningError(f"active corpus {logical_name} is not serving")
    return dict(zip(("collection_name","corpus_version_id","manifest_digest","capture_cutoff_sequence","active_revision","state"), row))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL"))
    parser.add_argument("--exports", default="chat_logs")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--embedder-url", default=os.getenv("EMBEDDER_URL"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument(
        "--simulation-input",
        type=Path,
        help=(
            "read work/records/manifest/points/source_corpus from JSON; "
            "does not connect to Postgres or Qdrant and cannot persist"
        ),
    )
    args = parser.parse_args()
    if args.simulation_input and args.persist:
        parser.error("--simulation-input cannot be combined with --persist")
    if not args.simulation_input and (not args.database_url or not args.qdrant_url):
        parser.error("--database-url and --qdrant-url are required")
    if args.simulation_input:
        simulation = json.loads(args.simulation_input.read_text(encoding="utf-8"))
        work = [WorkItem(**row) for row in simulation.get("work", [])]
        records = simulation.get("records", [])
        manifest = simulation.get("manifest", [])
        points = [
            (str(row["point_id"]), row.get("payload", {}))
            for row in simulation.get("points", [])
        ]
        source_corpus = simulation.get("source_corpus")
    else:
        import psycopg
        from qdrant_client import QdrantClient
        with psycopg.connect(args.database_url) as connection:
            work, live, manifest, source_corpus = load_postgres(
                connection, collection=args.collection
            )
        records = parse_all_exports(args.exports) + live
        points = scan_qdrant(QdrantClient(url=args.qdrant_url), args.collection)
    plan = create_shadow_plan(
        work, records, manifest, points, args.collection,
        source_corpus=source_corpus,
    )
    measurement = None
    if args.embedder_url:
        measurement = (
            embed_shadow(plan, args.embedder_url)
            if plan["replacement_point_count"]
            else benchmark_embedder(points, args.embedder_url)
        )
    rendered = render_plan(
        plan,
        measurement,
        source_corpus_current=True if source_corpus else None,
    )
    if args.persist:
        with psycopg.connect(args.database_url) as connection:
            persist_plan(connection, rendered)
    output = json.dumps(rendered, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
