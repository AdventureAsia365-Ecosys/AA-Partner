"""The visa tracker (AA-1070 item crm-16; crm-054 ``crm.visa_template``, ``crm.visa_application``,
``crm.visa_checklist_item``; crm-055 the status moves).

**Templates** are the agency's own guidance per country and visa type (processing days, fee, the documents a
traveller collects, the official https page). The agency keeps them: AA gives no visa advice (root ADR 0019 b6, the
AI boundary of AA-820), so nothing here is pre-filled by AA.

**Applications**: one per traveller, country and trip, made by an agent on a booked trip (:data:`OPEN_STAGES`):
:func:`suggest` lists the trip's destination countries that differ from each traveller's nationality (opened
server-side, never returned) and have no application yet; :func:`create` copies the template's documents into the
checklist. Status ``to_do -> collecting -> submitted -> approved | refused`` (refused -> collecting: apply again),
``not_needed``, ``cancelled`` (the moves are crm-055's ``crm.status_transition`` rows; an illegal one answers 409
``invalid_transition``). The granted visa's number and validity are sealed (``pii:government_id``) and only an
approved application carries them; lists show the number masked (last 3 characters).

**Deadline**: by default the trip's start minus the template's processing days minus :data:`DEADLINE_MARGIN_DAYS`;
the hourly workflow tick opens one ``visa_deadline_overdue`` task per trip while an open application (to_do /
collecting) is past it (:func:`overdue_tasks`).

**Traveller portal** (:mod:`app.travel_ops.portal`): each traveller sees their own applications and ticks their
checklist; the agency's checklist items stay agency wording.

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.intake import stage1
from app.tenancy import AnyContext
from app.vault import Vault
from app.vault.errors import DecryptionDenied

VISA_TYPES = ("evisa", "visa_on_arrival", "embassy", "visa_exempt", "transit", "other")
STATUSES = ("to_do", "collecting", "submitted", "approved", "refused", "not_needed", "cancelled")
OPEN_STATUSES = ("to_do", "collecting")
OPEN_STAGES = ("booked", "pre_departure", "travelling")
# trip.destinations codes (app.intake.stage1.DESTINATIONS) -> ISO 3166-1 alpha-2 (as app.duty.service).
DESTINATION_COUNTRY = {
    "vietnam": "VN",
    "laos": "LA",
    "cambodia": "KH",
    "thailand": "TH",
    "myanmar": "MM",
    "bhutan": "BT",
    "nepal": "NP",
    "indonesia": "ID",
}
OVERDUE_TASK = "visa_deadline_overdue"
DEADLINE_MARGIN_DAYS = 14
MAX_DOCUMENTS = 30
MAX_TEMPLATES = 200
MAX_ITEMS = 40
TABLE = "visa_application"
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_URL = re.compile(r"^https://[^\s\x00-\x1f\x7f]+$")
# crm-055's crm.status_transition rows (the trigger refuses anything else with a check_violation).
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "to_do": ("collecting", "submitted", "not_needed", "cancelled"),
    "collecting": ("to_do", "submitted", "not_needed", "cancelled"),
    "submitted": ("approved", "refused", "collecting", "cancelled"),
    "refused": ("collecting", "cancelled"),
    "not_needed": ("to_do",),
    "approved": ("cancelled",),
    "cancelled": (),
}

TEMPLATE_COLUMNS = """
visa_template_id, country, visa_type, processing_days, fee, fee_currency, documents, official_url, active, updated_at
"""
TEMPLATES_SQL = f"""
SELECT {TEMPLATE_COLUMNS} FROM crm.visa_template WHERE deleted_at IS NULL ORDER BY country, visa_type LIMIT $1
"""  # noqa: S608 — fixed text
TEMPLATE_SQL = f"SELECT {TEMPLATE_COLUMNS} FROM crm.visa_template WHERE visa_template_id = $1 AND deleted_at IS NULL"  # noqa: S608
SETTINGS_ROW_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
TEMPLATE_INSERT_SQL = """
INSERT INTO crm.visa_template (country, visa_type, processing_days, fee, fee_currency, documents, official_url, active)
VALUES ($1, $2, $3, $4::numeric, $5, $6::jsonb, $7, $8)
RETURNING visa_template_id
"""
TEMPLATE_UPDATE_SQL = """
UPDATE crm.visa_template
   SET country = $2, visa_type = $3, processing_days = $4, fee = $5::numeric, fee_currency = $6, documents = $7::jsonb,
       official_url = $8, active = $9
 WHERE visa_template_id = $1
"""
TEMPLATE_DELETE_SQL = "DELETE FROM crm.visa_template WHERE visa_template_id = $1"
TEMPLATE_FOR_SQL = f"""
SELECT {TEMPLATE_COLUMNS} FROM crm.visa_template
 WHERE country = $1 AND ($2::text IS NULL OR visa_type = $2) AND active AND deleted_at IS NULL
 ORDER BY visa_type LIMIT 1
"""  # noqa: S608 — fixed text
TRIP_SQL = "SELECT trip_id, stage, start_date, destinations FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
PARTY_SQL = """
SELECT tt.traveler_id, tt.role, t.display_name_enc, t.nationality_enc, t.passport_country_enc
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
APP_COLUMNS = """
a.visa_application_id, a.trip_id, a.traveler_id, a.visa_template_id, a.country, a.visa_type, a.status, a.deadline,
a.submitted_on, a.decided_on, a.visa_no_enc, a.valid_from_enc, a.valid_until_enc, a.updated_at
"""
APPS_SQL = f"""
SELECT {APP_COLUMNS} FROM crm.visa_application a
 WHERE a.trip_id = $1 AND a.deleted_at IS NULL AND ($2::uuid IS NULL OR a.traveler_id = $2)
 ORDER BY a.country, a.created_at, a.visa_application_id
"""  # noqa: S608 — fixed text
APP_SQL = f"SELECT {APP_COLUMNS} FROM crm.visa_application a WHERE a.visa_application_id = $1 AND a.deleted_at IS NULL"  # noqa: S608
APP_INSERT_SQL = """
INSERT INTO crm.visa_application (visa_application_id, trip_id, traveler_id, visa_template_id, country, visa_type,
                                  deadline)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
APP_UPDATE_SQL = """
UPDATE crm.visa_application
   SET status = $2, deadline = $3, submitted_on = $4, decided_on = $5, visa_no_enc = $6, valid_from_enc = $7,
       valid_until_enc = $8, visa_type = $9
 WHERE visa_application_id = $1
"""
APP_DELETE_SQL = "DELETE FROM crm.visa_application WHERE visa_application_id = $1"
ITEMS_SQL = """
SELECT visa_checklist_item_id, visa_application_id, label, sort, done, done_at, done_by_kind
  FROM crm.visa_checklist_item
 WHERE visa_application_id = ANY($1::uuid[]) AND deleted_at IS NULL
 ORDER BY visa_application_id, sort, created_at, visa_checklist_item_id
"""
ITEM_INSERT_SQL = """
INSERT INTO crm.visa_checklist_item (visa_application_id, label, sort) VALUES ($1, $2, $3)
RETURNING visa_checklist_item_id
"""
ITEM_SQL = """
SELECT i.visa_checklist_item_id, i.visa_application_id, a.trip_id, a.traveler_id
  FROM crm.visa_checklist_item i
  JOIN crm.visa_application a ON a.visa_application_id = i.visa_application_id AND a.deleted_at IS NULL
 WHERE i.visa_checklist_item_id = $1 AND i.deleted_at IS NULL
"""
ITEM_DONE_SQL = """
UPDATE crm.visa_checklist_item
   SET done = $2, done_at = CASE WHEN $2 THEN now() END, done_by_kind = CASE WHEN $2 THEN $3 END
 WHERE visa_checklist_item_id = $1
"""
ITEM_DELETE_SQL = "DELETE FROM crm.visa_checklist_item WHERE visa_checklist_item_id = $1"
ITEM_COUNT_SQL = "SELECT count(*) FROM crm.visa_checklist_item WHERE visa_application_id = $1 AND deleted_at IS NULL"
OVERDUE_SQL = """
SELECT a.trip_id, count(*)::int AS n
  FROM crm.visa_application a
  JOIN crm.trip t ON t.trip_id = a.trip_id AND t.deleted_at IS NULL AND t.stage = ANY($2::text[])
 WHERE a.deleted_at IS NULL AND a.status = ANY($3::text[]) AND a.deadline < $1::date
 GROUP BY a.trip_id
 ORDER BY a.trip_id
 LIMIT $4
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, 'created', now() FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


# ── checks ───────────────────────────────────────────────────────────────────────────────────────


def _country(value: Any, field: str = "country") -> str:
    code = str(value or "").strip().upper()
    if not _COUNTRY.match(code):
        raise Invalid(f"invalid_{field}", "a two-letter country code (ISO 3166-1)")
    return code


def _visa_type(value: Any) -> str:
    if value not in VISA_TYPES:
        raise Invalid("invalid_visa_type", ", ".join(VISA_TYPES))
    return str(value)


def _label(value: Any) -> str:
    try:
        clean = stage1.clean_text(value, 200)
    except ValueError:
        clean = None
    if not clean:
        raise Invalid("invalid_label", "1 to 200 characters")
    return clean


def _documents(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_DOCUMENTS:
        raise Invalid("invalid_documents", f"a list of at most {MAX_DOCUMENTS} documents")
    return [_label(v) for v in value]


def _fee(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        fee = Decimal(str(value))
    except InvalidOperation:
        raise Invalid("invalid_fee", "an amount from 0") from None
    if not fee.is_finite() or fee < 0 or fee > Decimal("99999999.99") or fee != fee.quantize(Decimal("0.01")):
        raise Invalid("invalid_fee", "an amount from 0, two decimals at most")
    return fee


def template_values(body: dict[str, Any]) -> tuple[Any, ...]:
    """The checked columns of a template, in :data:`TEMPLATE_INSERT_SQL` order."""
    days = body.get("processing_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 365):
        raise Invalid("invalid_processing_days", "0 to 365")
    fee = _fee(body.get("fee"))
    currency = body.get("fee_currency") or None
    if (fee is None) != (currency is None):
        raise Invalid("fee_needs_currency", "a fee and its currency, or neither")
    if currency is not None and not _CURRENCY.match(str(currency)):
        raise Invalid("invalid_fee_currency", "a three-letter currency code")
    url = body.get("official_url") or None
    if url is not None and (not isinstance(url, str) or len(url) > 500 or not _URL.match(url)):
        raise Invalid("invalid_official_url", "an https:// address")
    active = body.get("active", True)
    if not isinstance(active, bool):
        raise Invalid("invalid_active", "true or false")
    return (_country(body.get("country")), _visa_type(body.get("visa_type")), days,
            str(fee) if fee is not None else None, currency, json.dumps(_documents(body.get("documents"))), url,
            active)  # fmt: skip


def mask(number: str | None) -> str | None:
    """A visa number in a list: the last three characters only."""
    if not number:
        return None
    return "•••" + number[-3:] if len(number) > 3 else "•••"


def default_deadline(start: dt.date | None, processing_days: int | None) -> dt.date | None:
    if start is None:
        return None
    return start - dt.timedelta(days=(processing_days or 0) + DEADLINE_MARGIN_DAYS)


def destination_countries(destinations: list[str] | None) -> list[str]:
    seen: list[str] = []
    for code in destinations or []:
        iso = DESTINATION_COUNTRY.get(code)
        if iso and iso not in seen:
            seen.append(iso)
    return seen


# ── templates ────────────────────────────────────────────────────────────────────────────────────


def _documents_out(raw: Any) -> list[str]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    return [str(v) for v in value or []]


def template_view(row: Any) -> dict[str, Any]:
    return {
        "visa_template_id": str(row["visa_template_id"]),
        "country": row["country"],
        "visa_type": row["visa_type"],
        "processing_days": row["processing_days"],
        "fee": str(row["fee"]) if row["fee"] is not None else None,
        "fee_currency": row["fee_currency"],
        "documents": _documents_out(row["documents"]),
        "official_url": row["official_url"],
        "active": row["active"],
    }


async def list_templates(ctx: AnyContext) -> dict[str, Any]:
    rows = await ctx.conn.fetch(TEMPLATES_SQL, MAX_TEMPLATES)
    return {"templates": [template_view(r) for r in rows], "visa_types": list(VISA_TYPES)}


async def create_template(ctx: AnyContext, body: dict[str, Any]) -> dict[str, Any]:
    values = template_values(body)
    await ctx.conn.execute(SETTINGS_ROW_SQL)
    try:
        async with ctx.conn.transaction():
            template_id = await ctx.conn.fetchval(TEMPLATE_INSERT_SQL, *values)
    except asyncpg.UniqueViolationError:
        raise Conflict("visa_template_exists", "this country and visa type already has a template") from None
    ctx.audit("create", "allowed", "visa_template", str(template_id))
    return template_view(await ctx.conn.fetchrow(TEMPLATE_SQL, template_id))


async def update_template(ctx: AnyContext, template_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(TEMPLATE_SQL, template_id)
    if row is None:
        raise NotFound("visa_template_not_found")
    merged = {**template_view(row), **{k: v for k, v in changes.items() if k in TEMPLATE_KEYS}}
    values = template_values(merged)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(TEMPLATE_UPDATE_SQL, template_id, *values)
    except asyncpg.UniqueViolationError:
        raise Conflict("visa_template_exists", "this country and visa type already has a template") from None
    ctx.audit("update", "allowed", "visa_template", str(template_id), fields=tuple(sorted(changes)))
    return template_view(await ctx.conn.fetchrow(TEMPLATE_SQL, template_id))


TEMPLATE_KEYS = ("country", "visa_type", "processing_days", "fee", "fee_currency", "documents", "official_url",
                 "active")  # fmt: skip


async def delete_template(ctx: AnyContext, template_id: uuid.UUID) -> dict[str, Any]:
    """A template goes; the applications made from it keep their copy (no foreign key)."""
    if await ctx.conn.fetchrow(TEMPLATE_SQL, template_id) is None:
        raise NotFound("visa_template_not_found")
    await ctx.conn.execute(TEMPLATE_DELETE_SQL, template_id)
    ctx.audit("delete", "allowed", "visa_template", str(template_id))
    return {"visa_template_id": str(template_id), "deleted": True}


# ── applications ─────────────────────────────────────────────────────────────────────────────────


async def _open(vault: Vault, row: Any, column: str) -> str | None:
    blob = row[column]
    if blob is None:
        return None
    return await vault.decrypt(TABLE, column, row["visa_application_id"], blob)


async def application_view(
    vault: Vault, row: Any, items: list[Any], *, full: bool = False, names: dict[uuid.UUID, str] | None = None
) -> dict[str, Any]:
    """One application. ``full``: the visa number in clear (the agent's detail); else masked."""
    number = await _open(vault, row, "visa_no_enc")
    out = {
        "visa_application_id": str(row["visa_application_id"]),
        "trip_id": str(row["trip_id"]),
        "traveler_id": str(row["traveler_id"]),
        "traveler_name": (names or {}).get(row["traveler_id"]),
        "visa_template_id": str(row["visa_template_id"]) if row["visa_template_id"] else None,
        "country": row["country"],
        "visa_type": row["visa_type"],
        "status": row["status"],
        "deadline": _iso(row["deadline"]),
        "submitted_on": _iso(row["submitted_on"]),
        "decided_on": _iso(row["decided_on"]),
        "visa_no": number if full else mask(number),
        "valid_from": await _open(vault, row, "valid_from_enc"),
        "valid_until": await _open(vault, row, "valid_until_enc"),
        "checklist": [
            {
                "visa_checklist_item_id": str(i["visa_checklist_item_id"]),
                "label": i["label"],
                "done": i["done"],
                "done_by_kind": i["done_by_kind"],
            }
            for i in items
        ],
    }
    out["checklist_done"] = sum(1 for i in items if i["done"])
    return out


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _items(ctx: AnyContext, app_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Any]]:
    out: dict[uuid.UUID, list[Any]] = {a: [] for a in app_ids}
    if app_ids:
        for r in await ctx.conn.fetch(ITEMS_SQL, app_ids):
            out.setdefault(r["visa_application_id"], []).append(r)
    return out


async def _trip(ctx: AnyContext, trip_id: uuid.UUID) -> Any:
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    return trip


async def _names(ctx: AnyContext, vault: Vault, party: list[Any]) -> dict[uuid.UUID, str]:
    return {
        r["traveler_id"]: await vault.decrypt("traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"])
        for r in party
    }


async def trip_applications(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, *, traveler_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """The applications of a trip (or of one traveller on it: the portal), each with its checklist."""
    trip = await _trip(ctx, trip_id)
    rows = list(await ctx.conn.fetch(APPS_SQL, trip_id, traveler_id))
    items = await _items(ctx, [r["visa_application_id"] for r in rows])
    names = await _names(ctx, vault, list(await ctx.conn.fetch(PARTY_SQL, trip_id))) if traveler_id is None else {}
    apps = [await application_view(vault, r, items[r["visa_application_id"]], names=names) for r in rows]
    if apps:
        ctx.audit("list_view", "allowed", "visa_application", None, fields=("visa",),
                  reason="traveller_portal" if traveler_id else "trip_visas",
                  object_ids=tuple(a["visa_application_id"] for a in apps)[:200])  # fmt: skip
    return {
        "trip_id": str(trip_id),
        "can_add": trip["stage"] in OPEN_STAGES,
        "applications": apps,
        "visa_types": list(VISA_TYPES),
        "statuses": list(STATUSES),
    }


async def _nationality(vault: Vault, row: Any) -> str | None:
    for column in ("nationality_enc", "passport_country_enc"):
        if row[column] is not None:
            try:
                return await vault.decrypt("traveler", column, row["traveler_id"], row[column])
            except DecryptionDenied:
                return None
    return None


async def suggest(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID) -> dict[str, Any]:
    """For each traveller and destination country of the trip that is not their nationality (or not known) and has no
    application yet: the suggestion, with the agency's active template for that country when it has one."""
    trip = await _trip(ctx, trip_id)
    countries = destination_countries(trip["destinations"])
    party = list(await ctx.conn.fetch(PARTY_SQL, trip_id))
    names = await _names(ctx, vault, party)
    have = {(r["traveler_id"], r["country"]) for r in await ctx.conn.fetch(APPS_SQL, trip_id, None)}
    templates: dict[str, Any] = {}
    for country in countries:
        templates[country] = await ctx.conn.fetchrow(TEMPLATE_FOR_SQL, country, None)
    out = []
    for person in party:
        own = await _nationality(vault, person)
        for country in countries:
            if country == own or (person["traveler_id"], country) in have:
                continue
            tpl = templates.get(country)
            out.append(
                {
                    "traveler_id": str(person["traveler_id"]),
                    "traveler_name": names[person["traveler_id"]],
                    "country": country,
                    "nationality_known": own is not None,
                    "template": template_view(tpl) if tpl is not None else None,
                }
            )
    return {"trip_id": str(trip_id), "countries": countries, "can_add": trip["stage"] in OPEN_STAGES,
            "suggestions": out}  # fmt: skip


async def create(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    trip = await _trip(ctx, trip_id)
    if trip["stage"] not in OPEN_STAGES:
        raise Conflict("trip_not_booked", "visas are tracked on a booked trip")
    try:
        traveler_id = uuid.UUID(str(body.get("traveler_id")))
    except ValueError:
        raise Invalid("invalid_traveler_id", "a traveller of the trip") from None
    party = {r["traveler_id"]: r for r in await ctx.conn.fetch(PARTY_SQL, trip_id)}
    if traveler_id not in party:
        raise Invalid("traveler_not_on_trip", "a traveller of the trip")
    country = _country(body.get("country"))
    template = None
    if body.get("visa_template_id"):
        try:
            template = await ctx.conn.fetchrow(TEMPLATE_SQL, uuid.UUID(str(body["visa_template_id"])))
        except ValueError:
            template = None
        if template is None:
            raise NotFound("visa_template_not_found")
        if template["country"] != country:
            raise Invalid("template_other_country", "the template is for another country")
    visa_type = _visa_type(body.get("visa_type") or (template["visa_type"] if template else None))
    if template is None:
        template = await ctx.conn.fetchrow(TEMPLATE_FOR_SQL, country, visa_type)
    deadline = body.get("deadline")
    if deadline is None:
        deadline = default_deadline(trip["start_date"], template["processing_days"] if template else None)
    elif not isinstance(deadline, dt.date):
        raise Invalid("invalid_deadline", "a date")
    app_id = uuid.uuid4()
    try:
        async with ctx.conn.transaction():
            template_id = template["visa_template_id"] if template else None
            await ctx.conn.execute(APP_INSERT_SQL, app_id, trip_id, traveler_id, template_id, country, visa_type,
                                   deadline)  # fmt: skip
            documents = _documents_out(template["documents"]) if template else []
            for sort, label in enumerate(documents[:MAX_ITEMS]):
                await ctx.conn.execute(ITEM_INSERT_SQL, app_id, label, sort)
    except asyncpg.UniqueViolationError:
        raise Conflict("visa_application_exists", "this traveller already has a visa for this country") from None
    ctx.audit("create", "allowed", "visa_application", str(app_id), object_ids=(str(trip_id),))
    return await get(ctx, vault, app_id)


async def get(ctx: AnyContext, vault: Vault, app_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(APP_SQL, app_id)
    if row is None:
        raise NotFound("visa_application_not_found")
    items = await _items(ctx, [app_id])
    ctx.audit("view", "allowed", "visa_application", str(app_id), fields=("visa_no", "valid_from", "valid_until"))
    return await application_view(vault, row, items[app_id], full=True)


def _date(value: Any, field: str) -> dt.date | None:
    if value is None or value == "" or isinstance(value, dt.date):
        return value or None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "a date") from None


async def update(ctx: AnyContext, vault: Vault, app_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    """Move the status, set the dates, the deadline and (approved only) the granted visa's number and validity."""
    row = await ctx.conn.fetchrow(APP_SQL, app_id)
    if row is None:
        raise NotFound("visa_application_not_found")
    status = changes.get("status") or row["status"]
    if status not in STATUSES:
        raise Invalid("invalid_status", ", ".join(STATUSES))
    if status != row["status"] and status not in TRANSITIONS[row["status"]]:
        raise Conflict("invalid_transition", f"{row['status']} cannot move to {status}")
    visa_type = _visa_type(changes["visa_type"]) if changes.get("visa_type") else row["visa_type"]
    deadline = _date(changes["deadline"], "deadline") if "deadline" in changes else row["deadline"]
    submitted = _date(changes["submitted_on"], "submitted_on") if "submitted_on" in changes else row["submitted_on"]
    decided = _date(changes["decided_on"], "decided_on") if "decided_on" in changes else row["decided_on"]
    today = dt.date.today()
    if status in ("submitted", "approved", "refused") and submitted is None:
        submitted = today
    if status in ("approved", "refused") and decided is None:
        decided = today
    if status in ("to_do", "collecting", "not_needed"):
        submitted, decided = None, None
    if decided is not None and submitted is not None and decided < submitted:
        raise Invalid("decided_before_submitted", "the decision comes after the submission")

    async def sealed(field: str) -> bytes | None:
        column = f"{field}_enc"
        if field not in changes:
            return row[column] if status == "approved" else None
        value = changes[field]
        if value in (None, ""):
            return None
        if status != "approved":
            raise Invalid("visa_details_need_approval", "the visa number and validity go on an approved visa")
        if field == "visa_no":
            text = re.sub(r"\s", "", str(value)).upper()
            if not re.fullmatch(r"[A-Z0-9-]{3,40}", text):
                raise Invalid("invalid_visa_no", "3 to 40 letters, digits or -")
        else:
            text = _date(value, field).isoformat()  # type: ignore[union-attr]
        return await vault.encrypt(TABLE, column, app_id, text)

    numbers = [await sealed(f) for f in ("visa_no", "valid_from", "valid_until")]
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(APP_UPDATE_SQL, app_id, status, deadline, submitted, decided, *numbers, visa_type)
    except asyncpg.CheckViolationError as exc:
        if "transition" in str(exc):
            raise Conflict("invalid_transition", f"{row['status']} cannot move to {status}") from None
        raise Invalid("invalid_visa_application", "check the dates and the status") from None
    ctx.audit("update", "allowed", "visa_application", str(app_id), fields=tuple(sorted(changes)))
    return await get(ctx, vault, app_id)


async def delete(ctx: AnyContext, app_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(APP_SQL, app_id)
    if row is None:
        raise NotFound("visa_application_not_found")
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(APP_DELETE_SQL, app_id)
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == "CRM10":
            raise Conflict("legal_hold", "this trip is on legal hold") from None
        raise
    ctx.audit("delete", "allowed", "visa_application", str(app_id))
    return {"visa_application_id": str(app_id), "deleted": True}


async def add_item(ctx: AnyContext, app_id: uuid.UUID, label: Any) -> dict[str, Any]:
    if await ctx.conn.fetchrow(APP_SQL, app_id) is None:
        raise NotFound("visa_application_not_found")
    count = int(await ctx.conn.fetchval(ITEM_COUNT_SQL, app_id) or 0)
    if count >= MAX_ITEMS:
        raise Conflict("too_many_items", f"at most {MAX_ITEMS} documents per visa")
    item_id = await ctx.conn.fetchval(ITEM_INSERT_SQL, app_id, _label(label), min(count, 100))
    ctx.audit("create", "allowed", "visa_checklist_item", str(item_id))
    return {"visa_checklist_item_id": str(item_id), "visa_application_id": str(app_id)}


async def set_item(
    ctx: AnyContext, item_id: uuid.UUID, done: bool, *, by: str = "member", traveler_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Tick or untick one document. ``by='traveler'`` (the portal): only an item of that traveller's application."""
    row = await ctx.conn.fetchrow(ITEM_SQL, item_id)
    if row is None or (traveler_id is not None and row["traveler_id"] != traveler_id):
        raise NotFound("visa_checklist_item_not_found")
    await ctx.conn.execute(ITEM_DONE_SQL, item_id, bool(done), by)
    ctx.audit("update", "allowed", "visa_checklist_item", str(item_id), fields=("done",))
    return {"visa_checklist_item_id": str(item_id), "done": bool(done), "done_by_kind": by if done else None}


async def delete_item(ctx: AnyContext, item_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchrow(ITEM_SQL, item_id) is None:
        raise NotFound("visa_checklist_item_not_found")
    await ctx.conn.execute(ITEM_DELETE_SQL, item_id)
    ctx.audit("delete", "allowed", "visa_checklist_item", str(item_id))
    return {"visa_checklist_item_id": str(item_id), "deleted": True}


async def overdue_tasks(ctx: AnyContext, today: dt.date, limit: int = 500) -> Counter[str]:
    """One open ``visa_deadline_overdue`` task per live trip with an open application past its deadline."""
    counts: Counter[str] = Counter()
    for r in await ctx.conn.fetch(OVERDUE_SQL, today, list(OPEN_STAGES), list(OPEN_STATUSES), limit):
        counts["visa_overdue_trips"] += 1
        status = await ctx.conn.execute(TASK_SQL, r["trip_id"], OVERDUE_TASK)
        counts["visa_tasks_opened"] += int(str(status).split()[-1] == "1") if status else 0
    return counts
