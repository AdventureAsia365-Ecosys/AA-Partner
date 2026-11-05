"""The one-agency restore drill (AA-969 part 2, root ADR 0019 d16; runbook: root
``docs/ops/runbooks/pilot-tenant-restore-drill.md``).

ADR 0019 d16: RDS point-in-time recovery restores the whole database of all four apps, never one agency, so the pilot
agency is recovered from its own nightly export (``crm_pilot_backup``, :mod:`app.lifecycle.pilot`). This drill proves
that export restores. Inside the CRM API container (it has the ``crm_app`` DSN, the KMS key and the documents bucket),
through ECS Exec on ``aa-crm-dev-api``::

    python -m app.portability.restore_drill                       # the pilot agency, its newest nightly export
    python -m app.portability.restore_drill --tenant <uuid>       # another agency (its newest nightly export)
    python -m app.portability.restore_drill --tenant <uuid> --export <uuid>   # one given ready export

Steps, in one transaction of the agency (:func:`app.tenancy.lifecycle_tx`, actor :data:`ACTOR`):

1. the export row: ``ready`` and not expired, scope ``tenant`` (the newest one the nightly backup requested, unless
   ``--export`` names one);
2. the sealed object from the bucket: its SHA-256 must equal ``object_sha256``; opened under the agency ``export`` DEK;
   the ZIP's SHA-256 must equal ``content_sha256``; every file listed in ``SHA256SUMS`` must match;
3. **restore into a throwaway schema**: every table of ``manifest.json`` becomes a temporary table (the session's own
   ``pg_temp`` schema, ``ON COMMIT DROP``: nothing outlives the drill, no other session can see it) with the export's
   columns (a column the live table has keeps its type, so a value that no longer parses fails the drill; a decrypted
   ``*_enc`` value and any other type become ``text``), loaded from ``json/<table>.json`` with
   ``jsonb_populate_recordset``;
4. **compare row counts**: per table the manifest's count, the restored count, the live count now (``crm.<table>``
   under the agency's row security) and how many restored primary keys still exist live.

Pass (``ok``): both digests and every checksum match, and for every table restored = manifest. The live counts are
reported, not judged: the agency kept working after the export. ``complete`` is stricter: ``ok`` and no document of the
agency left out of the backup for a reason that loses it (``documents.skipped_by_reason``: past the export's size cap,
its object missing or damaged, documents not configured; the documents of S3 are sealed under the agency keys, so a
document the backup does not hold cannot be recovered from it). Exit 0 = complete, 3 = the rows restored but the
backup is incomplete, 1 = failed. ``not_in_backup`` lists what the export never holds (vendor credentials, the agency
keys, the audit log, blind indexes and token columns: :mod:`app.portability.tables`); the runbook says how a restore
gets each back. Output: one JSON object of ids, table names and counts
(never a value of a row, so the drill reads no personal data out of the container; root ``.claude/hooks`` and the Infra
run-script guard keep agent reads to counts too). The drill writes one ``restore_drill`` audit event in the agency's
log and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import sys
import uuid
import zipfile
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from app.documents.store import DocumentStore
from app.errors import NotFound, Unavailable
from app.lifecycle import pilot
from app.portability import tables as T
from app.tenancy import LifecycleContext, lifecycle_tx
from app.vault import Vault

if TYPE_CHECKING:  # pragma: no cover
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

ACTOR = "job:crm_restore_drill"
LATEST_SQL = """
SELECT * FROM crm.export_job
 WHERE scope = 'tenant' AND status = 'ready' AND requested_by = $1 AND expires_at > now()
 ORDER BY completed_at DESC, export_job_id LIMIT 1
"""
ONE_SQL = "SELECT * FROM crm.export_job WHERE export_job_id = $1 AND scope = 'tenant'"
LIVE_COLUMNS_SQL = """
SELECT a.attname::text AS name, format_type(a.atttypid, a.atttypmod) AS type
  FROM pg_catalog.pg_attribute a
 WHERE a.attrelid = to_regclass('crm.' || quote_ident($1)) AND a.attnum > 0 AND NOT a.attisdropped
"""
# The live types a restored column keeps (with their [] array forms); anything else is restored as text.
KEPT_TYPES = frozenset(
    {
        "uuid",
        "text",
        "boolean",
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "date",
        "timestamp with time zone",
        "timestamp without time zone",
        "jsonb",
        "json",
    }
)


# A document the backup should hold but does not: the backup cannot bring it back.
INCOMPLETE_DOCUMENT_REASONS = frozenset(
    {"skipped_size_cap", "object_missing", "skipped_integrity", "documents_not_configured"}
)
DOCUMENTS_MANIFEST = "documents/manifest.csv"


@dataclass
class TableResult:
    table: str
    manifest_rows: int
    restored_rows: int
    live_rows: int | None
    live_pk_matched: int | None

    @property
    def ok(self) -> bool:
        return self.restored_rows == self.manifest_rows


class DrillError(Exception):
    """A drill that cannot go on: the code says why (no export, a digest mismatch, …)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def kept_type(live: str | None) -> str:
    if live is None:
        return "text"
    base = live[:-2] if live.endswith("[]") else live
    if base.startswith("character varying") or base.startswith("character(") or base.startswith("numeric("):
        return live
    return live if base in KEPT_TYPES else "text"


def verify_zip(content: bytes) -> tuple[zipfile.ZipFile, dict[str, Any], int]:
    """(the ZIP, its manifest, how many files the checksums cover). Raises ``checksum_mismatch`` / ``zip_shape``."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        sums = zf.read("SHA256SUMS").decode("ascii")
        manifest = json.loads(zf.read("manifest.json"))
    except (KeyError, ValueError, zipfile.BadZipFile):
        raise DrillError("zip_shape") from None
    checked = 0
    for line in sums.splitlines():
        digest, _, path = line.partition("  ")
        try:
            data = zf.read(path)
        except KeyError:
            raise DrillError("checksum_missing_file") from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise DrillError("checksum_mismatch")
        checked += 1
    if not isinstance(manifest.get("tables"), list):
        raise DrillError("zip_shape")
    return zf, manifest, checked


def document_skips(zf: zipfile.ZipFile, manifest: dict[str, Any]) -> dict[str, int]:
    """How many documents the export left out, per ``skipped_reason`` of ``documents/manifest.csv`` (checksummed)."""
    path = str((manifest.get("documents") or {}).get("manifest") or DOCUMENTS_MANIFEST)
    try:
        text = zf.read(path).decode("utf-8-sig")  # the export CSVs start with a BOM
    except KeyError:
        return {}
    counts: dict[str, int] = {}
    for line in csv.DictReader(io.StringIO(text)):
        if str(line.get("included", "")).strip().lower() in ("true", "1"):
            continue
        reason = (line.get("skipped_reason") or "unknown").strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def lost_documents(skips: dict[str, int]) -> int:
    return sum(n for reason, n in skips.items() if reason in INCOMPLETE_DOCUMENT_REASONS or reason == "unknown")


def not_in_backup() -> dict[str, list[str]]:
    """What a portability export never holds (so neither does the nightly backup)."""
    return {
        "tables": sorted(T.NEVER_EXPORT_TABLES),
        "columns": sorted(T.NEVER_EXPORT_COLUMNS) + ["*_bidx"],
    }


def exit_code(report: dict[str, Any]) -> int:
    """0 = complete, 3 = every table restored but documents are missing from the backup, 1 = failed."""
    if not report.get("ok"):
        return 1
    return 0 if report.get("complete") else 3


async def _export_row(ctx: LifecycleContext, export_id: uuid.UUID | None) -> Any:
    if export_id is None:
        row = await ctx.conn.fetchrow(LATEST_SQL, pilot.ACTOR)
    else:
        row = await ctx.conn.fetchrow(ONE_SQL, export_id)
    if row is None:
        raise DrillError("no_export")
    if row["status"] != "ready" or row["object_key"] is None:
        raise DrillError("export_not_ready")
    return row


async def open_export(ctx: LifecycleContext, vault: Vault, store: DocumentStore, row: Any) -> bytes:
    try:
        sealed = await store.get(row["object_key"])
    except Unavailable:  # the bucket rule took it (past 7 days) or it was withdrawn
        raise DrillError("object_missing") from None
    if hashlib.sha256(sealed).hexdigest() != row["object_sha256"]:
        raise DrillError("object_digest_mismatch")
    content = await vault.decrypt_bytes("export_job", "object", row["export_job_id"], sealed, purpose="export")
    if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
        raise DrillError("content_digest_mismatch")
    return content


async def restore_table(ctx: LifecycleContext, n: int, entry: dict[str, Any], rows: list[dict[str, Any]],
                        pk: tuple[str, ...]) -> TableResult:  # fmt: skip
    """Load one table of the export into ``pg_temp.rd_<n>`` and count it (restored, live, primary keys live)."""
    table = str(entry["table"])
    live = {r["name"]: r["type"] for r in await ctx.conn.fetch(LIVE_COLUMNS_SQL, table)}
    columns: list[str] = []
    for r in rows:
        columns.extend(k for k in r if k not in columns)
    temp = f"rd_{n}"
    if columns:
        spec = ", ".join(f"{ident(c)} {kept_type(live.get(c))}" for c in columns)
    else:
        spec = "drill_empty boolean"
    await ctx.conn.execute(f"CREATE TEMP TABLE {temp} ({spec}) ON COMMIT DROP")
    if rows:
        await ctx.conn.execute(
            f"INSERT INTO pg_temp.{temp} SELECT * FROM jsonb_populate_recordset(NULL::pg_temp.{temp}, $1::jsonb)",  # noqa: S608
            json.dumps(rows),
        )
    restored = int(await ctx.conn.fetchval(f"SELECT count(*) FROM pg_temp.{temp}"))  # noqa: S608
    live_rows = matched = None
    if live:
        live_rows = int(await ctx.conn.fetchval(f"SELECT count(*) FROM crm.{ident(table)}"))  # noqa: S608
        if pk and all(c in columns and kept_type(live.get(c)) == live.get(c) for c in pk):
            on = " AND ".join(f"l.{ident(c)} = d.{ident(c)}" for c in pk)
            matched = int(
                await ctx.conn.fetchval(
                    f"SELECT count(*) FROM pg_temp.{temp} d WHERE EXISTS "  # noqa: S608
                    f"(SELECT 1 FROM crm.{ident(table)} l WHERE {on})"
                )
            )
    return TableResult(table, int(entry.get("rows", -1)), restored, live_rows, matched)


async def drill(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    store: DocumentStore,
    tenant_id: uuid.UUID,
    export_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Run the drill for one agency; the report (ids, table names, counts). Raises :class:`DrillError`."""
    async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
        row = await _export_row(ctx, export_id)
        content = await open_export(ctx, Vault(keys, ctx), store, row)
        zf, manifest, checked = verify_zip(content)
        del content
        pks = {t.name: t.pk for t in await _catalog(ctx)}
        results: list[TableResult] = []
        for n, entry in enumerate(manifest["tables"]):
            rows = json.loads(zf.read(entry["json"]))
            results.append(await restore_table(ctx, n, entry, rows, pks.get(str(entry["table"]), ())))
            del rows
        ok = all(r.ok for r in results)
        skips = document_skips(zf, manifest)
        lost = lost_documents(skips)
        complete = ok and lost == 0
        reason = "mismatch" if not ok else ("ok" if complete else "incomplete_documents")
        ctx.audit("restore_drill", "allowed", "export_job", str(row["export_job_id"]), reason=reason)
    return {
        "tenant_id": str(tenant_id),
        "export_id": str(row["export_job_id"]),
        "export_completed_at": row["completed_at"].isoformat(),
        "schema_version": manifest.get("schema_version"),
        "checksums_verified": checked,
        "documents": {k: manifest.get("documents", {}).get(k) for k in ("included", "skipped", "max_bytes")}
        | {"skipped_by_reason": skips, "lost": lost},
        "tables": [asdict(r) | {"ok": r.ok} for r in results],
        "table_count": len(results),
        "manifest_rows": sum(r.manifest_rows for r in results),
        "restored_rows": sum(r.restored_rows for r in results),
        "mismatched_tables": [r.table for r in results if not r.ok],
        "ok": ok,
        "complete": complete,
        "not_in_backup": not_in_backup(),
    }


async def _catalog(ctx: LifecycleContext) -> list[T.Table]:
    from app.portability import builder

    return await builder.catalog(ctx)


async def pilot_tenant(pool: asyncpg.Pool) -> uuid.UUID:
    found = await pilot.pilot_agencies(pool)
    if not found:
        raise DrillError("no_pilot_agency")
    return found[0]


def failure(exc: BaseException) -> dict[str, Any]:
    """The report line of a failed drill: the error code of a known refusal; for anything else (a database error, a
    connection error) only its class and SQLSTATE, never its message (a database error text can quote a row value)."""
    if isinstance(exc, DrillError | NotFound | Unavailable):
        return {"ok": False, "error": exc.code}
    out: dict[str, Any] = {"ok": False, "error": "unexpected", "error_class": type(exc).__name__}
    state = getattr(exc, "sqlstate", None)
    if isinstance(state, str) and state:
        out["sqlstate"] = state
    return out


async def _main(argv: list[str]) -> int:  # pragma: no cover — the container entry point; drill() is tested
    from app.main import build_document_store
    from app.settings import load_settings
    from app.vault import build_key_provider

    parser = argparse.ArgumentParser(
        prog="python -m app.portability.restore_drill", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--tenant", type=uuid.UUID, default=None, help="the agency (default: the pilot agency)")
    parser.add_argument("--export", type=uuid.UUID, default=None, help="a ready export (default: the newest nightly)")
    args = parser.parse_args(argv)
    settings = load_settings()
    keys, store = build_key_provider(settings), build_document_store(settings)
    if keys is None or store is None or settings.database_url is None:
        print(json.dumps({"ok": False, "error": "not_configured"}))
        return 2
    pool = None
    try:
        pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            ssl=settings.db_ssl,
            min_size=1,
            max_size=2,
            command_timeout=300,
            server_settings={"application_name": "crm-restore-drill"},
        )
        tenant = args.tenant or await pilot_tenant(pool)
        report = await drill(pool, keys, store, tenant, args.export)
    except Exception as exc:  # noqa: BLE001 — every error is reported by class and SQLSTATE only (no values)
        print(json.dumps(failure(exc)))
        return 1
    finally:
        if pool is not None:
            await pool.close()
    print(json.dumps(report, sort_keys=True))
    return exit_code(report)


def cli(argv: list[str] | None = None) -> int:  # pragma: no cover
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
