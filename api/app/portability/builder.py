"""The portability ZIP (AA-967, design §4.10, spec module 18 "full export CSV / JSON").

Layout (``schema_version`` 1)::

    manifest.json              format, schema_version, scope, subject, agency, created_at, the crm schema versions,
                               per table: rows, files and their SHA-256; documents: included / skipped; notes
    json/<table>.json          a JSON array of the table's rows (decrypted, ``*_enc`` -> the name without the suffix)
    csv/<table>.csv            the same rows as CSV (UTF-8 with BOM, header = the JSON keys, nested values as JSON,
                               spreadsheet formula guard)
    documents/manifest.csv     one line per document row: id, trip, client, kind, file name, type, size, SHA-256 of
                               the content, included, path inside the ZIP (or why it was left out)
    documents/files/<id>/<name>  the decrypted file
    SHA256SUMS                 ``sha256sum -c`` format, every other file

Entries carry a fixed timestamp, so the same rows give the same bytes. The ZIP's own SHA-256 is stored on
``crm.export_job.content_sha256`` and shown on the Settings page. Documents are included up to
:data:`DOCUMENTS_MAX_BYTES` in total (the rest is listed with ``skipped_size_cap``; the manifest names the cap); a file
whose stored copy does not match its digest is listed ``skipped_integrity``, never included.

Memory (AA-967 review): the ZIP is built in memory inside the CRM API task (512 MB, also the in-process crm.job
worker). At the peak the ZIP, its sealed copy and one document (sealed and open, at most 7 MiB each) are alive at once,
and the download holds the sealed and the open ZIP: about three times the documents cap plus the tables. The cap is
25 MiB so that stays near 100 MB; raising it needs a streamed build (spooled file, chunked seal, multipart upload) or
more task memory, not just a bigger number (``tests/unit/test_portability.py`` guards both).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.documents.store import DocumentStore
from app.errors import Unavailable
from app.portability import tables as T
from app.tenancy import LifecycleContext
from app.vault import Vault

SCHEMA_VERSION = 1
FORMAT = "aa-partner-crm-export"
DOCUMENTS_MAX_BYTES = 25 * 1024 * 1024
ZIP_DATE = (2026, 1, 1, 0, 0, 0)
VERSIONS_SQL = "SELECT coalesce(array_agg(version ORDER BY version), '{}') FROM crm.v_schema_versions"

Progress = Callable[[int, int], Awaitable[None]]


@dataclass
class Built:
    content: bytes
    sha256: str
    row_counts: dict[str, int]
    documents_included: int
    documents_skipped: int
    manifest: dict[str, Any] = field(default_factory=dict)


def _guard(text: str) -> str:
    # Spreadsheet formula guard (as the access-log CSV, app.audit_log).
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict | list):
        return _guard(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return _guard(str(value))


def to_csv(rows: list[dict[str, Any]], header: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for r in rows:
        writer.writerow([_csv_cell(r.get(h)) for h in header])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or "file"
    return cleaned[:120]


class _Zip:
    def __init__(self) -> None:
        self.buf = io.BytesIO()
        self.zf = zipfile.ZipFile(self.buf, "w", compression=zipfile.ZIP_DEFLATED)
        self.sums: dict[str, str] = {}

    def add(self, path: str, data: bytes) -> str:
        info = zipfile.ZipInfo(path, date_time=ZIP_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        self.zf.writestr(info, data)
        digest = hashlib.sha256(data).hexdigest()
        self.sums[path] = digest
        return digest

    def close(self) -> bytes:
        lines = "".join(f"{d}  {p}\n" for p, d in sorted(self.sums.items()))
        info = zipfile.ZipInfo("SHA256SUMS", date_time=ZIP_DATE)
        info.external_attr = 0o644 << 16
        self.zf.writestr(info, lines.encode("ascii"))
        self.zf.close()
        content = self.buf.getvalue()
        self.buf.close()  # free the buffer now: only one copy of the ZIP stays alive
        return content


async def catalog(ctx: LifecycleContext) -> list[T.Table]:
    return [
        T.Table(r["name"], tuple(r["pk"]), tuple(r["columns"]), frozenset(r["json_columns"]))
        for r in await ctx.conn.fetch(T.TABLES_SQL)
    ]


async def table_rows(
    ctx: LifecycleContext,
    vault: Vault,
    table: T.Table,
    where: str | None = None,
    arg: Any = None,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The table's rows in this agency (FORCE RLS), decrypted, as JSON-ready dicts ordered by the primary key
    (``columns``: only those, default every exported column)."""
    order = ", ".join(table.pk) if table.pk else "1"
    sql = f"SELECT * FROM crm.{table.name}"  # noqa: S608 — names come from the catalog, not from a caller
    if where is not None:
        rows = await ctx.conn.fetch(f"{sql} WHERE {where} ORDER BY {order}", arg)
    else:
        rows = await ctx.conn.fetch(f"{sql} ORDER BY {order}")
    columns = table.exported_columns() if columns is None else columns
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {}
        for column in columns:
            value = r[column]
            if column.endswith("_enc"):
                if value is not None and table.row_key:
                    value = await vault.decrypt(
                        table.name, column, r[table.row_key], value, purpose=table.purpose(column)
                    )
                elif value is not None:
                    value = None  # no single-column key = no AAD row id: never guessed
            elif column in table.json_columns:
                value = T.parse_json_column(value)
            item[table.out_name(column)] = T.json_value(value)
        out.append(item)
    return out


async def build(
    ctx: LifecycleContext,
    vault: Vault,
    store: DocumentStore | None,
    *,
    scope: str,
    subject_id: uuid.UUID,
    export_id: uuid.UUID,
    profile_token: uuid.UUID | None = None,
    progress: Progress | None = None,
    documents_max_bytes: int = DOCUMENTS_MAX_BYTES,
) -> Built:
    """Every exported table of the agency (scope ``tenant``) or every row about one client (scope ``traveler``), and
    the documents those rows name, as the ZIP above."""
    found = T.exported(await catalog(ctx))
    plan: list[tuple[T.Table, str | None, Any, list[str]]] = []
    for table in found:
        if scope == "tenant":
            plan.append((table, None, None, table.exported_columns()))
            continue
        q = T.traveler_query(table)
        if q is None:
            continue
        where, kind = q
        if kind == "token":
            if profile_token is None:
                continue
            plan.append((table, where, profile_token, T.client_columns(table)))
        else:
            plan.append((table, where, subject_id, T.client_columns(table)))

    zipped = _Zip()
    counts: dict[str, int] = {}
    files: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    total = len(plan)
    done = 0
    for table, where, arg, columns in plan:
        rows = await table_rows(ctx, vault, table, where, arg, columns)
        counts[table.name] = len(rows)
        header = [table.out_name(c) for c in columns]
        j = zipped.add(f"json/{table.name}.json", json.dumps(rows, ensure_ascii=False, indent=1).encode("utf-8"))
        c = zipped.add(f"csv/{table.name}.csv", to_csv(rows, header))
        files.append(
            {
                "table": table.name,
                "rows": len(rows),
                "json": f"json/{table.name}.json",
                "json_sha256": j,
                "csv": f"csv/{table.name}.csv",
                "csv_sha256": c,
            }
        )
        if table.name == "document":
            documents = rows
        done += 1
        if progress is not None:
            await progress(done, total + len(documents))
    total += len(documents)

    included = skipped = 0
    budget = documents_max_bytes
    doc_lines: list[dict[str, Any]] = []
    raw_keys: dict[uuid.UUID, tuple[str, bytes | None]] = {}
    if documents:
        ids = [uuid.UUID(d["document_id"]) for d in documents]
        raw_keys = {
            r["document_id"]: (r["s3_key"], bytes(r["sha256"]) if r["sha256"] is not None else None)
            for r in await ctx.conn.fetch(
                "SELECT document_id, s3_key, sha256 FROM crm.document WHERE document_id = ANY($1::uuid[])", ids
            )
        }
    for d in documents:
        doc_id = uuid.UUID(d["document_id"])
        name = str(d.get("filename") or "file")
        line = {
            "document_id": str(doc_id),
            "trip_id": d.get("trip_id"),
            "traveler_id": d.get("traveler_id"),
            "kind": d.get("kind"),
            "file_name": name,
            "content_type": d.get("content_type"),
            "size_bytes": d.get("size_bytes"),
            "content_sha256": None,
            "included": False,
            "path": None,
            "skipped_reason": None,
        }
        s3_key, digest = raw_keys.get(doc_id, (None, None))
        size = int(d.get("size_bytes") or 0)
        if store is None or s3_key is None:
            line["skipped_reason"] = "documents_not_configured" if store is None else "no_object"
        elif size > budget:
            line["skipped_reason"] = "skipped_size_cap"
        else:
            try:
                sealed = await store.get(s3_key)
            except Unavailable:
                sealed = None
            if sealed is None:
                line["skipped_reason"] = "object_missing"
            elif digest is not None and hashlib.sha256(sealed).digest() != digest:
                line["skipped_reason"] = "skipped_integrity"
            else:
                content = await vault.decrypt_bytes("document", "content", doc_id, sealed, purpose="docs")
                path = f"documents/files/{doc_id}/{_safe_name(name)}"
                line |= {"content_sha256": zipped.add(path, content), "included": True, "path": path}
                budget -= len(content)
        if line["included"]:
            included += 1
        else:
            skipped += 1
        doc_lines.append(line)
        done += 1
        if progress is not None and (done % 10 == 0 or done == total):
            await progress(done, total)
    doc_header = list(doc_lines[0].keys()) if doc_lines else ["document_id", "file_name", "included", "path"]
    zipped.add("documents/manifest.csv", to_csv(doc_lines, doc_header))

    manifest = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "subject_id": str(subject_id),
        "tenant_id": str(ctx.tenant_id),
        "export_id": str(export_id),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "crm_schema_versions": list(await ctx.conn.fetchval(VERSIONS_SQL)),
        "tables": files,
        "documents": {
            "manifest": "documents/manifest.csv",
            "included": included,
            "skipped": skipped,
            "max_bytes": documents_max_bytes,
        },
        "checksums": "SHA256SUMS",
        "notes": [
            "personal_fields_decrypted",
            "index_and_token_columns_left_out",
            "audit_log_downloaded_separately",
            "vendor_secrets_never_exported",
            f"documents_capped_at_{documents_max_bytes // (1024 * 1024)}_mib_rest_listed_skipped_size_cap",
        ],
    }
    zipped.add("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
    content = zipped.close()
    return Built(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        row_counts=counts,
        documents_included=included,
        documents_skipped=skipped,
        manifest=manifest,
    )
