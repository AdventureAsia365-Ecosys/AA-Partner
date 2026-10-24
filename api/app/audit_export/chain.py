"""The export object and its hash chain (format 1; pure, no I/O).

One object per agency per UTC hour, written byte for byte the same to both Object Lock buckets:

- line 1: ``{"manifest": {...}, "manifest_sha256": "<hex>"}``;
- then one line per event of the hour, ordered by ``(at, event_id)``.

Every line is canonical JSON (keys sorted, no spaces, ASCII only) followed by ``\\n``. The hashes:

- ``content_sha256`` = SHA-256 of the event lines (empty hour = SHA-256 of nothing);
- ``manifest_sha256`` = SHA-256 of the canonical manifest: ``format``, ``tenant_id``, ``seq``, ``period_start``,
  ``period_end``, ``event_count``, ``content_sha256``, ``prev_manifest_sha256`` (64 zeros for ``seq`` 1);
- ``object_sha256`` = SHA-256 of the whole object.

Because the object carries its own manifest, the chain can be walked from the archive alone once the database rows
are gone (12 months): each object names the previous manifest hash. Rebuilding an object from the same rows and the
same previous hash gives the same bytes, which is what makes a retried export idempotent (card CRM-12 E test).
The events are codes and random ids only (``crm.audit_event`` CHECKs): no personal data reaches the archive.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

FORMAT = 1
ZERO_HASH = "0" * 64
PERIOD = timedelta(hours=1)
KEY_PREFIX = "audit/v1/tenants"
EVENT_COLUMNS = (
    "event_id",
    "tenant_id",
    "at",
    "actor_kind",
    "actor_ref",
    "action",
    "outcome",
    "object_kind",
    "object_id",
    "fields",
    "reason",
    "request_id",
    "object_ids",
    "created_by",
)


def canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iso(ts: datetime) -> str:
    """UTC, microseconds, ``Z``: one spelling per instant, whatever the session time zone."""
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def hour_floor(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def event_line(row: Mapping[str, Any]) -> bytes:
    """One event as canonical JSON. ``row`` has the :data:`EVENT_COLUMNS` (an asyncpg Record or a dict)."""
    out: dict[str, Any] = {}
    for col in EVENT_COLUMNS:
        value = row[col]
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = iso(value)
        elif isinstance(value, list | tuple):
            value = list(value)
        out[col] = value
    return canonical(out)


def object_key(tenant_id: uuid.UUID, period_start: datetime, seq: int) -> str:
    s = period_start.astimezone(UTC)
    return f"{KEY_PREFIX}/{tenant_id}/{s:%Y/%m/%d}/{s:%H}00Z-{seq:010d}.jsonl"


@dataclass(frozen=True)
class ExportObject:
    tenant_id: uuid.UUID
    seq: int
    period_start: datetime
    period_end: datetime
    event_count: int
    content_sha256: str
    prev_manifest_sha256: str
    manifest_sha256: str
    object_key: str
    body: bytes
    object_sha256: str


def manifest_fields(
    tenant_id: uuid.UUID,
    seq: int,
    period_start: datetime,
    period_end: datetime,
    event_count: int,
    content_sha256: str,
    prev_manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "tenant_id": str(tenant_id),
        "seq": seq,
        "period_start": iso(period_start),
        "period_end": iso(period_end),
        "event_count": event_count,
        "content_sha256": content_sha256,
        "prev_manifest_sha256": prev_manifest_sha256,
    }


def build_object(
    tenant_id: uuid.UUID,
    seq: int,
    period_start: datetime,
    period_end: datetime,
    events: Iterable[Mapping[str, Any]],
    prev_manifest_sha256: str,
) -> ExportObject:
    if seq < 1 or (seq == 1) != (prev_manifest_sha256 == ZERO_HASH):
        raise ValueError("seq 1 has the zero previous hash, every later seq a real one")
    lines = [event_line(e) for e in events]
    content = b"".join(lines)
    content_sha = sha256_hex(content)
    manifest = manifest_fields(tenant_id, seq, period_start, period_end, len(lines), content_sha, prev_manifest_sha256)
    manifest_sha = sha256_hex(canonical(manifest))
    body = canonical({"manifest": manifest, "manifest_sha256": manifest_sha}) + content
    return ExportObject(
        tenant_id=tenant_id,
        seq=seq,
        period_start=period_start,
        period_end=period_end,
        event_count=len(lines),
        content_sha256=content_sha,
        prev_manifest_sha256=prev_manifest_sha256,
        manifest_sha256=manifest_sha,
        object_key=object_key(tenant_id, period_start, seq),
        body=body,
        object_sha256=sha256_hex(body),
    )


@dataclass(frozen=True)
class ParsedObject:
    manifest: dict[str, Any]
    manifest_sha256: str
    content: bytes


def parse_object(body: bytes) -> ParsedObject:
    """Split an archived object into its manifest and event lines (no hash checked here)."""
    head, sep, content = body.partition(b"\n")
    if not sep:
        raise ValueError("not an audit export object")
    first = json.loads(head)
    if not isinstance(first, dict) or not isinstance(first.get("manifest"), dict):
        raise ValueError("not an audit export object")
    return ParsedObject(
        manifest=first["manifest"], manifest_sha256=str(first.get("manifest_sha256", "")), content=content
    )


def object_problems(body: bytes) -> list[str]:
    """What is wrong with one archived object on its own (manifest hash, content hash, count)."""
    try:
        parsed = parse_object(body)
    except (ValueError, json.JSONDecodeError):
        return ["object_unreadable"]
    problems = []
    if sha256_hex(canonical(parsed.manifest)) != parsed.manifest_sha256:
        problems.append("manifest_hash")
    if sha256_hex(parsed.content) != parsed.manifest.get("content_sha256"):
        problems.append("content_hash")
    lines = parsed.content.count(b"\n")
    if lines != parsed.manifest.get("event_count"):
        problems.append("event_count")
    return problems
