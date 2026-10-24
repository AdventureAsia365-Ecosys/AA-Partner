"""Verify the audit export of an agency: the database rows, the manifest chain and both archived copies agree.

``python -m app.audit_export.verify [--tenant <uuid> ...] [--since <iso>] [--no-objects]`` inside the API container
(it has the ``crm_app`` DSN and the task role): one JSON line per agency (ids, counts and problem codes only), exit 1
when any agency has a problem. Read-only: it never writes the database or the buckets. The daily ``crm_audit_retention``
job runs the same check incrementally (from ``verified_until``) before any partition may be dropped.

Checks, per manifest of the agency, in ``seq`` order:

- **chain**: ``seq`` has no gap, ``prev_manifest_sha256`` is the previous row's hash, periods are contiguous
  (``seq_gap``, ``chain_broken``, ``period_gap``), and the manifest hash recomputes (``manifest_hash``);
- **database**: while the hour is still in the database (its partition not dropped), the hour's events are read
  again and the object rebuilt: a row deleted, added or changed since the export changes the content hash
  (``content_mismatch``; card CRM-12 E test "the verify script detects a deleted row");
- **archive**: each tier's object exists (``object_missing:<tier>``), is byte for byte the exported object
  (``object_mismatch:<tier>``) and is consistent on its own (``object_<problem>:<tier>``);
- once per agency: no event older than the first exported hour (``unexported_rows``), and the registry's ``last_seq`` /
  ``exported_until`` match the last manifest (``registry_mismatch``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from app.audit_export import registry
from app.audit_export.archive import TIERS, ArchiveMissing, AuditArchive, build_audit_archive
from app.audit_export.chain import ZERO_HASH, canonical, event_line, manifest_fields, object_problems, sha256_hex
from app.audit_export.export import read_events
from app.tenancy import ExportContext, audit_export_tx

MANIFESTS_SQL = """
SELECT seq, period_start, period_end, event_count, content_sha256, prev_manifest_sha256, manifest_sha256, object_key,
       object_sha256
  FROM crm.audit_export_manifest
 WHERE period_end > $1
 ORDER BY seq
"""
REGISTRY_SQL = "SELECT exported_until, last_seq FROM crm.audit_export_tenant WHERE tenant_id = $1"
# The start of the oldest audit partition (NULL = the crm-003 partition from MINVALUE: the database holds everything).
FLOOR_SQL = """
SELECT CASE WHEN bool_or(range_start IS NULL) THEN NULL ELSE min(range_start) END,
       count(*) > 0
  FROM crm.audit_partition_bounds()
"""
BEFORE_FIRST_SQL = "SELECT count(*) FROM crm.audit_event WHERE at < $1"
EARLIEST = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


@dataclass
class VerifyReport:
    tenant_id: uuid.UUID
    checked: int = 0
    checked_until: datetime | None = None
    problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, code: str, seq: int | None = None) -> None:
        self.problems.append({"code": code, "seq": seq})

    def as_json(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "ok": self.ok,
            "checked_count": self.checked,
            "checked_until": self.checked_until.isoformat() if self.checked_until else None,
            "problems": self.problems,
        }


async def _check(
    ctx: ExportContext, archive: AuditArchive | None, since: datetime | None, report: VerifyReport
) -> None:
    rows = await ctx.conn.fetch(MANIFESTS_SQL, since or EARLIEST)
    floor, has_partitions = await ctx.conn.fetchrow(FLOOR_SQL)
    reg = await ctx.conn.fetchrow(REGISTRY_SQL, ctx.tenant_id)
    if since is None and rows and has_partitions:
        if await ctx.conn.fetchval(BEFORE_FIRST_SQL, rows[0]["period_start"]):
            report.add("unexported_rows")
    prev = None
    for m in rows:
        seq = m["seq"]
        if prev is not None:
            if seq != prev["seq"] + 1:
                report.add("seq_gap", seq)
            if m["prev_manifest_sha256"] != prev["manifest_sha256"]:
                report.add("chain_broken", seq)
            if m["period_start"] != prev["period_end"]:
                report.add("period_gap", seq)
        elif since is None and (seq != 1 or m["prev_manifest_sha256"] != ZERO_HASH):
            report.add("chain_broken", seq)
        fields_ = manifest_fields(
            ctx.tenant_id,
            seq,
            m["period_start"],
            m["period_end"],
            m["event_count"],
            m["content_sha256"],
            m["prev_manifest_sha256"],
        )
        if sha256_hex(canonical(fields_)) != m["manifest_sha256"]:
            report.add("manifest_hash", seq)
        if floor is None or m["period_start"] >= floor:
            lines = [event_line(e) for e in await read_events(ctx, m["period_start"], m["period_end"])]
            if sha256_hex(b"".join(lines)) != m["content_sha256"] or len(lines) != m["event_count"]:
                report.add("content_mismatch", seq)
        if archive is not None:
            for tier in TIERS:
                try:
                    body = await archive.get(tier, m["object_key"])
                except ArchiveMissing:
                    report.add(f"object_missing:{tier}", seq)
                    continue
                if sha256_hex(body) != m["object_sha256"]:
                    report.add(f"object_mismatch:{tier}", seq)
                report.problems.extend({"code": f"object_{p}:{tier}", "seq": seq} for p in object_problems(body))
        report.checked += 1
        report.checked_until = m["period_end"]
        prev = m
    if reg is not None and since is None:
        last_seq = rows[-1]["seq"] if rows else 0
        if reg["last_seq"] != last_seq or (rows and reg["exported_until"] != rows[-1]["period_end"]):
            report.add("registry_mismatch")


async def verify_tenant(
    pool: asyncpg.Pool, archive: AuditArchive | None, tenant_id: uuid.UUID, *, since: datetime | None = None
) -> VerifyReport:
    """Verify one agency. ``since``: only the manifests whose hour ends after it (the daily incremental run); None =
    every manifest still in the database. ``archive`` None = database and chain only."""
    report = VerifyReport(tenant_id)
    async with audit_export_tx(pool, tenant_id) as ctx:
        await _check(ctx, archive, since, report)
    return report


async def _main(argv: list[str]) -> int:
    from app.db import Database
    from app.settings import load_settings

    parser = argparse.ArgumentParser(prog="python -m app.audit_export.verify", description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", action="append", default=[], type=uuid.UUID, help="only this agency (repeatable)")
    parser.add_argument("--since", type=datetime.fromisoformat, default=None, help="only hours ending after this")
    parser.add_argument("--no-objects", action="store_true", help="skip the archived objects (database + chain)")
    args = parser.parse_args(argv)
    settings = load_settings()
    db = Database(settings)
    archive = None if args.no_objects else build_audit_archive(settings)
    failed = 0
    try:
        pool = await db.pool()
        tenants = args.tenant or [a.tenant_id for a in await registry.agencies(pool)]
        for tenant_id in tenants:
            report = await verify_tenant(pool, archive, tenant_id, since=args.since)
            failed += 0 if report.ok else 1
            print(json.dumps(report.as_json(), sort_keys=True))
    finally:
        await db.close()
    return 1 if failed else 0


def cli(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
