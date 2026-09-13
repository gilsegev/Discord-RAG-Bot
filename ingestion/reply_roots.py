"""Authoritative, fail-closed Discord reply-root resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RootResolution:
    """A trusted root, or a transient reason why no root can be trusted."""

    root_message_id: str | None
    failure_reason: str | None = None


def resolve_reply_root(
    message_id: str,
    records: Mapping[str, Mapping[str, Any]],
    channel_id: str | None = None,
) -> RootResolution:
    """Walk upward to a complete, same-channel root without guessing.

    ``failure_reason`` is diagnostic-only and must not be persisted in chunk or
    vector payloads.
    """
    current = str(message_id)
    expected_channel = str(channel_id) if channel_id is not None else None
    seen: set[str] = set()
    depth = 0

    while True:
        if current in seen:
            return RootResolution(None, "cycle")
        seen.add(current)

        record = records.get(current)
        if record is None:
            reason = (
                "missing_immediate_parent" if depth == 1
                else "missing_higher_ancestor"
            )
            return RootResolution(None, reason)

        record_channel = record.get("channel_id")
        if record_channel is None or (
            expected_channel is not None
            and str(record_channel) != expected_channel
        ):
            return RootResolution(None, "cross_channel")
        if expected_channel is None:
            expected_channel = str(record_channel)

        parent = record.get("parent_id")
        if not parent:
            return RootResolution(current)

        current = str(parent)
        depth += 1
