"""S-02 for AA ground operations (AA-956, card CRM-26; design v3 S-02, Fix B3, §4.9 "AA ops reads", Q1 3.9 / 3.10).

Every function opens :func:`app.tenancy.ops_tx` (actor ``ops:<staff sub>``, audit actor kind ``aa_ops``) for one agency
at a time; the route (``app/routers/staff.py``) has already checked the WORKFORCE group ``crm-ops``.

- :func:`board`: the live releases of every active or locked agency, departing from ``on - 14 days`` (still on the
  ground) to ``on + horizon``, and the withdrawals of the last 14 days (a cancelled trip: the notice to AA ops).
  **No personal data**: agency name, trip reference, dates, party size, completeness counts, Booking departure ids,
  the purge countdown, views. One ``ops_release_list`` event per agency listed.
- :func:`read`: one release document, opened under the agency's ``ops_release`` DEK. 404 ``release_not_found`` unless
  the release is live (not withdrawn, not purged) and the trip still has a confirmed AA line now (card R). Each view:
  ``view_count`` + 1 (the crm-034 guard lets this actor change nothing else) and one ``ops_release_view`` event, the
  agency's "AA ops viewed trip X".
- :func:`manifest`: the minimal supplier manifest (CSV, the fields the caller picks), watermarked on every row; one
  ``crm.supplier_manifest_export`` record (no personal data, the file is never stored) and one
  ``ops_manifest_export`` event.

Fix B3: there is no incident flag here. :func:`incident_admin_url` is the Booking staff admin's B2B desk (tab
"Departures & incidents"), where AA flags an incident on a departure; the board and the release show it with the
departure ids to look for.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import secrets
import uuid
from typing import Any

import asyncpg

from app.audit_export import registry
from app.auth.route_guard import Caller
from app.errors import Forbidden, Invalid, NotFound
from app.ops_release import service
from app.tenancy import OpsContext, ops_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

BOARD_PAST_DAYS = 14
BOARD_HORIZON_DAYS = 120
BOARD_MAX_PER_AGENCY = 200
# The manifest columns a supplier may receive (Q1 3.10 minimal set; never insurance, which is the agency's matter).
MANIFEST_FIELDS = ("name", "age_band", "dietary", "mobility", "arrival_flight", "departure_flight")
MANIFEST_DEFAULT = ("name", "age_band", "dietary", "mobility")
WATERMARK_PREFIX = "AAOPS"
INCIDENT_ADMIN_PATH = "/b2b-desk"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

AGENCY_NAME_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
# Live releases in the window, and the withdrawals of the last BOARD_PAST_DAYS days (the "AA ops get a notice" of a
# cancel, data-privacy-integrations §3.4 step 6: the row shows as withdrawn, without its document).
BOARD_SQL = """
SELECT r.ops_release_id, r.trip_id, r.payload_version, r.completeness, r.complete, r.traveller_count,
       r.departure_ids, r.start_date, r.end_date, r.purge_at, r.refreshed_at, r.t21_alerted_at, r.view_count,
       r.last_viewed_at, r.withdrawn_at, r.withdraw_reason
  FROM crm.ops_release r
 WHERE ((r.purged_at IS NULL AND r.withdrawn_at IS NULL
         AND (r.start_date IS NULL OR r.start_date BETWEEN $1 AND $2 OR (r.start_date < $1 AND r.end_date >= $1)))
        OR (r.withdrawn_at IS NOT NULL AND r.withdrawn_at >= $1::date))
 ORDER BY r.start_date NULLS LAST, r.ops_release_id
 LIMIT $3
"""
READ_SQL = """
SELECT r.ops_release_id, r.payload_enc, r.payload_version, r.fields_released, r.completeness, r.complete,
       r.traveller_count, r.departure_ids, r.bk_booking_ids, r.start_date, r.end_date, r.released_at, r.refreshed_at,
       r.purge_at, r.view_count
  FROM crm.ops_release r
 WHERE r.trip_id = $1 AND r.purged_at IS NULL AND r.withdrawn_at IS NULL
   AND EXISTS (SELECT 1 FROM crm.booking_line bl
                WHERE bl.trip_id = r.trip_id AND bl.supplier_kind = 'aa' AND bl.status = 'confirmed'
                  AND bl.deleted_at IS NULL)
"""
VIEW_SQL = """
UPDATE crm.ops_release SET view_count = view_count + 1, last_viewed_at = now() WHERE ops_release_id = $1
RETURNING view_count
"""
MANIFEST_SQL = """
INSERT INTO crm.supplier_manifest_export (ops_release_id, payload_version, fields, row_count, watermark)
VALUES ($1, $2, $3, $4, $5)
RETURNING supplier_manifest_export_id
"""


def incident_admin_url(booking_admin_url: str) -> str | None:
    """Fix B3: where AA flags an incident on a departure (the Booking staff admin), or None when not configured."""
    return f"{booking_admin_url}{INCIDENT_ADMIN_PATH}" if booking_admin_url else None


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _trip_ref(trip_id: uuid.UUID) -> str:
    return str(trip_id)[:8]


async def board(
    pool: asyncpg.Pool, caller: Caller, *, horizon_days: int = BOARD_HORIZON_DAYS, booking_admin_url: str = ""
) -> dict[str, Any]:
    """The S-02 list across agencies (no personal data).

    Every registered agency is visited (like the job and the other cross-agency staff reads): the registry lists
    offboarded and trashed agencies too, in tenant_id order, so a cap on that list hid whole agencies (their live
    releases and their withdrawal notices) by the luck of their id, not by any rule AA ops could see."""
    items: list[dict[str, Any]] = []
    for agency in await registry.agencies(pool):
        try:
            async with ops_tx(pool, caller, agency.tenant_id) as ctx:
                items.extend(await _agency_board(ctx, horizon_days))
        except NotFound:
            continue  # offboarding or gone
        except Forbidden:
            continue  # AA-1019: the Dev-only E2E staff identity, an agency that is not synthetic
    items.sort(key=lambda i: (i["start_date"] or "9999-12-31", i["agency_name"] or "", i["trip_id"]))
    return {
        "items": items,
        "horizon_days": horizon_days,
        "incident_admin_url": incident_admin_url(booking_admin_url),
    }


async def _agency_board(ctx: OpsContext, horizon_days: int) -> list[dict[str, Any]]:
    on = await service.today(ctx)
    rows = await ctx.conn.fetch(
        BOARD_SQL, on - dt.timedelta(days=BOARD_PAST_DAYS), on + dt.timedelta(days=horizon_days), BOARD_MAX_PER_AGENCY
    )
    if not rows:
        return []
    name = await ctx.conn.fetchval(AGENCY_NAME_SQL, ctx.tenant_id)
    ctx.audit("ops_release_list", "allowed", "ops_release", None, reason="board",
              object_ids=tuple(str(r["trip_id"]) for r in rows))  # fmt: skip
    return [
        {
            "tenant_id": str(ctx.tenant_id),
            "agency_name": name,
            "agency_status": ctx.status,
            "trip_id": str(r["trip_id"]),
            "trip_ref": _trip_ref(r["trip_id"]),
            "status": "withdrawn" if r["withdrawn_at"] else "live",
            "withdrawn_at": _iso(r["withdrawn_at"]),
            "withdraw_reason": r["withdraw_reason"],
            "start_date": _iso(r["start_date"]),
            "end_date": _iso(r["end_date"]),
            "traveller_count": r["traveller_count"],
            "complete": r["complete"],
            "completeness": _json(r["completeness"]),
            "departure_ids": list(r["departure_ids"]),
            "payload_version": r["payload_version"],
            "refreshed_at": _iso(r["refreshed_at"]),
            "t21_alerted": r["t21_alerted_at"] is not None,
            "purge_at": _iso(r["purge_at"]),
            "days_to_purge": (r["purge_at"] - on).days if r["purge_at"] and not r["withdrawn_at"] else None,
            "view_count": r["view_count"],
            "last_viewed_at": _iso(r["last_viewed_at"]),
        }
        for r in rows
    ]


async def _live(ctx: OpsContext, trip_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(READ_SQL, trip_id)
    if row is None:
        raise NotFound("release_not_found", "no ops release for this trip (no confirmed AA line, or withdrawn)")
    return row


async def _open(ctx: OpsContext, keys: KeyProvider, row: Any) -> dict[str, Any]:
    vault = Vault(keys, ctx)  # type: ignore[arg-type] — same shape as SystemContext (tenant_id, audit, DEK memo)
    text = await vault.decrypt("ops_release", "payload_enc", row["ops_release_id"], row["payload_enc"],
                               purpose="ops_release")  # fmt: skip
    return json.loads(text)


async def read(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    caller: Caller,
    tenant_id: uuid.UUID,
    trip_id: uuid.UUID,
    *,
    booking_admin_url: str = "",
) -> dict[str, Any]:
    """One release document for AA ops, audited as "AA ops viewed trip X"."""
    async with ops_tx(pool, caller, tenant_id) as ctx:
        row = await _live(ctx, trip_id)
        doc = await _open(ctx, keys, row)
        views = await ctx.conn.fetchval(VIEW_SQL, row["ops_release_id"])
        ctx.audit("ops_release_view", "allowed", "trip", str(trip_id), fields=tuple(row["fields_released"]),
                  reason="ops_release")  # fmt: skip
        on = await service.today(ctx)
        log.info("crm_ops_release_viewed tenant_id=%s ops_release_id=%s", tenant_id, row["ops_release_id"])
        return {
            "tenant_id": str(tenant_id),
            "trip_id": str(trip_id),
            "ops_release_id": str(row["ops_release_id"]),
            "payload_version": row["payload_version"],
            "fields_released": list(row["fields_released"]),
            "completeness": _json(row["completeness"]),
            "complete": row["complete"],
            "departure_ids": list(row["departure_ids"]),
            "bk_booking_ids": list(row["bk_booking_ids"]),
            "released_at": _iso(row["released_at"]),
            "refreshed_at": _iso(row["refreshed_at"]),
            "purge_at": _iso(row["purge_at"]),
            "days_to_purge": (row["purge_at"] - on).days if row["purge_at"] else None,
            "view_count": views,
            "incident_admin_url": incident_admin_url(booking_admin_url),
            "document": doc,
        }


def watermark() -> str:
    return f"{WATERMARK_PREFIX}-{secrets.token_hex(5).upper()}"


def _cell(value: str | None) -> str:
    """CSV formula injection: a cell a spreadsheet would run starts with a quote instead."""
    text = value or ""
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _flight(f: dict[str, Any] | None) -> str:
    if not f:
        return ""
    return " ".join(str(p) for p in (f.get("flight_no"), f.get("airport"), f.get("sched_local"), f.get("tz")) if p)


def _name(person: dict[str, Any]) -> str:
    full = " ".join(p for p in (person.get("given_name"), person.get("family_name")) if p)
    return full or person.get("name") or ""


def manifest_csv(doc: dict[str, Any], fields: tuple[str, ...], mark: str) -> tuple[str, int]:
    """The CSV text and its row count. Columns: the watermark, the row number, then the picked fields."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["watermark", "no", *fields])
    rows = 0
    for i, person in enumerate(doc.get("travellers") or [], start=1):
        values = {
            "name": _name(person),
            "age_band": person.get("age_band") or "",
            "dietary": " ".join(person.get("dietary") or []),
            "mobility": " ".join(person.get("mobility") or []),
            "arrival_flight": _flight(person.get("arrival")),
            "departure_flight": _flight(person.get("departure")),
        }
        w.writerow([mark, str(i), *(_cell(values[f]) for f in fields)])
        rows += 1
    return out.getvalue(), rows


def pick_fields(fields: list[str] | None) -> tuple[str, ...]:
    if not fields:
        return MANIFEST_DEFAULT
    unknown = sorted(set(fields) - set(MANIFEST_FIELDS))
    if unknown:
        raise Invalid("manifest_field_unknown", ", ".join(MANIFEST_FIELDS), fields=unknown)
    return tuple(f for f in MANIFEST_FIELDS if f in set(fields))


async def manifest(
    pool: asyncpg.Pool, keys: KeyProvider, caller: Caller, tenant_id: uuid.UUID, trip_id: uuid.UUID, fields: list[str]
) -> dict[str, Any]:
    """The minimal supplier manifest of one release: ``{filename, watermark, row_count, csv}``."""
    picked = pick_fields(fields)
    async with ops_tx(pool, caller, tenant_id) as ctx:
        row = await _live(ctx, trip_id)
        doc = await _open(ctx, keys, row)
        mark = watermark()
        text, count = manifest_csv(doc, picked, mark)
        export_id = await ctx.conn.fetchval(MANIFEST_SQL, row["ops_release_id"], row["payload_version"], list(picked),
                                            count, mark)  # fmt: skip
        ctx.audit("ops_manifest_export", "allowed", "trip", str(trip_id), fields=picked, reason="supplier_manifest",
                  object_ids=(str(export_id),))  # fmt: skip
        log.info("crm_ops_manifest_exported tenant_id=%s ops_release_id=%s rows=%s", tenant_id, row["ops_release_id"],
                 count)  # fmt: skip
    return {
        "filename": f"manifest-{_trip_ref(trip_id)}-{mark}.csv",
        "watermark": mark,
        "row_count": count,
        "fields": list(picked),
        "csv": text,
    }
