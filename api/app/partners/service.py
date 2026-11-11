"""Sales partners and their commission, the agency side (AA-1071, crm-056; S275 study item crm-28).

- **Partners** (``crm.sales_partner``): kind (sub_agent, referrer, freelancer, ota), the name and contact under the
  vault DEK, the commission terms (percent / fixed per traveller / net rate, rate, basis revenue or margin, payout
  days). Every member lists them (the trip's partner picker: id, kind, name, status); the terms and the money only
  reach a role with ``view_margin``. Only an owner creates, edits, deactivates or deletes one
  (``sales_partner_manage``; the database refuses anyone else, CRM54). A partner that a trip, a commission or a code
  mail names is deactivated, never deleted (409 ``partner_in_use``); clearing its contact e-mail and phone removes
  the personal data it holds beyond its name.
- **Links** (``crm.sales_partner_link``): an owner issues the partner's intake link (one of the agency's active
  inquiry forms; the inquiry it sends is stamped with the partner) or its view link (the partner's own trip list
  after an e-mail code). The 43-character token is shown once (the web path ``/c/r/<token>`` or ``/c/a/<token>``),
  only its SHA-256 is kept; a new link of a purpose revokes the active one; at most 400 days.
- **The trip's partner** (``crm.trip.sales_partner_id``): any member sets or clears it on a trip that is not cancelled
  (an inactive partner is not offered); the database trigger then accrues, refreshes or cancels the trip's commission
  (``crm.partner_commission_sync``), whoever made the change.
- **Commissions** (``crm.partner_commission``): owner / agent read them (``view_margin``) with totals in the base
  currency; "Recalculate" refreshes an accrued row (the margin moves as bills arrive); an owner approves (as
  themselves, after a fresh recalculation), un-approves, marks paid (date + own reference) or cancels one
  (``partner_commission_approve``). An owner's cancel (like a trip cancel's) is a waiver: the sync accrues nothing new
  for that trip and partner until the trip leaves the booked stages, so a later stage move never brings it back. A
  per-partner statement is a CSV of its commissions (trip reference, travel date, pax, basis, amount, status, due and
  paid dates, payout reference), never a traveller's name.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import secrets
import uuid
from decimal import Decimal
from typing import Any

from app import cancellation
from app.errors import Conflict, Invalid, NotFound
from app.permissions import Action, decide
from app.tenancy import TenantContext
from app.vault import Vault

TABLE = "sales_partner"
KINDS = ("sub_agent", "referrer", "freelancer", "ota")
TYPES = ("percent", "fixed_per_pax", "net_rate")
BASES = ("revenue", "margin")
PURPOSES = ("intake", "view")
LINK_PATHS = {"intake": "/c/r/", "view": "/c/a/"}
DEFAULT_LINK_DAYS = 180
MAX_LINK_DAYS = 400
STATUSES = ("accrued", "approved", "paid", "cancelled")
OPEN = ("accrued", "approved")
MAX_NAME = 120
_PHONE = re.compile(r"^\+?[0-9 ()./-]{4,40}$")
_CONTROL = re.compile(r"[\u0000-\u001f\u007f]")
_REF = re.compile(r"^[^\u0000-\u001f\u007f]{1,120}$")

PARTNERS_SQL = """
SELECT p.sales_partner_id, p.kind, p.name_enc, p.contact_email_enc IS NOT NULL AS has_email,
       p.contact_phone_enc IS NOT NULL AS has_phone, p.commission_type, p.rate, p.currency, p.basis,
       p.payout_due_days, p.status, p.created_at,
       (SELECT count(*) FROM crm.trip t WHERE t.sales_partner_id = p.sales_partner_id AND t.deleted_at IS NULL)
           AS trip_count
  FROM crm.sales_partner p
 WHERE p.deleted_at IS NULL
 ORDER BY p.status, p.created_at, p.sales_partner_id
"""
PARTNER_SQL = """
SELECT sales_partner_id, kind, name_enc, contact_email_enc, contact_phone_enc, commission_type, rate, currency, basis,
       payout_due_days, status
  FROM crm.sales_partner WHERE sales_partner_id = $1 AND deleted_at IS NULL
"""
LINKS_SQL = """
SELECT link_id, sales_partner_id, purpose, intake_form_id, expires_at, last_used_at, use_count, created_at
  FROM crm.sales_partner_link
 WHERE status = 'active' AND expires_at > now() AND deleted_at IS NULL
"""
# Open commission totals per partner (base currency now); RESTRICTIVE: none for a role without view_margin.
OPEN_TOTALS_SQL = """
SELECT c.sales_partner_id, c.status, sum(c.amount_base) FILTER (WHERE c.base_currency = $1) AS amount_base,
       count(*) AS n
  FROM crm.partner_commission c
 WHERE c.status IN ('accrued', 'approved') AND c.deleted_at IS NULL
 GROUP BY c.sales_partner_id, c.status
"""
INSERT_SQL = """
INSERT INTO crm.sales_partner (sales_partner_id, kind, name_enc, contact_email_enc, contact_phone_enc, commission_type,
                               rate, currency, basis, payout_due_days)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""
# A commission row of any status counts (the partner's payout history: a paid row stays after the trip moves to
# another partner); the owner reads every row (partner_commission_margin_roles). crm-056 also refuses the cascade of an
# approved or paid row while its trip remains (partner_commission_kept).
IN_USE_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.trip WHERE sales_partner_id = $1)
    OR EXISTS (SELECT 1 FROM crm.partner_commission WHERE sales_partner_id = $1)
    OR EXISTS (SELECT 1 FROM crm.conversation WHERE sales_partner_id = $1)
    OR EXISTS (SELECT 1 FROM crm.dispatch_queue WHERE sales_partner_id = $1)
"""
DELETE_SQL = "DELETE FROM crm.sales_partner WHERE sales_partner_id = $1"
# A partner goes with the agency's settings row (crm-056 key): make sure there is one.
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
FORM_SQL = "SELECT 1 FROM crm.intake_form WHERE intake_form_id = $1 AND status = 'active' AND deleted_at IS NULL"
REVOKE_ACTIVE_SQL = """
UPDATE crm.sales_partner_link SET status = 'revoked', revoked_at = now()
 WHERE sales_partner_id = $1 AND purpose = $2 AND status = 'active'
"""
INSERT_LINK_SQL = """
INSERT INTO crm.sales_partner_link (link_id, sales_partner_id, purpose, intake_form_id, token_hash, expires_at)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(days => $6))
RETURNING expires_at
"""
REVOKE_LINK_SQL = """
UPDATE crm.sales_partner_link SET status = 'revoked', revoked_at = now()
 WHERE link_id = $1 AND sales_partner_id = $2 AND status = 'active'
RETURNING link_id
"""
TRIP_SQL = "SELECT trip_id, sales_partner_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE"
ACTIVE_PARTNER_SQL = (
    "SELECT 1 FROM crm.sales_partner WHERE sales_partner_id = $1 AND status = 'active' AND deleted_at IS NULL"
)
SET_TRIP_SQL = "UPDATE crm.trip SET sales_partner_id = $2 WHERE trip_id = $1"
SYNC_SQL = "SELECT crm.partner_commission_sync($1)"
BASE_SQL = "SELECT COALESCE((SELECT base_currency::text FROM crm.tenant_settings), 'USD')"
COMMISSION_COLUMNS = """
SELECT c.partner_commission_id, c.trip_id, c.sales_partner_id, c.commission_type, c.rate, c.basis, c.basis_amount,
       c.pax, c.amount, c.currency, c.base_currency, c.amount_base, c.computed_at, c.due_on, c.approved_at, c.paid_on,
       c.payout_ref, c.cancel_reason, c.status, c.created_at, t.start_date, t.end_date, t.stage
  FROM crm.partner_commission c JOIN crm.trip t ON t.trip_id = c.trip_id
"""
COMMISSIONS_SQL = (
    COMMISSION_COLUMNS
    + """ WHERE c.deleted_at IS NULL AND ($1::text[] IS NULL OR c.status = ANY ($1))
   AND ($2::uuid IS NULL OR c.sales_partner_id = $2) AND ($3::uuid IS NULL OR c.trip_id = $3)
 ORDER BY c.due_on NULLS LAST, c.created_at, c.partner_commission_id
 LIMIT 500"""
)
COMMISSION_SQL = COMMISSION_COLUMNS + " WHERE c.partner_commission_id = $1 AND c.deleted_at IS NULL FOR UPDATE OF c"
APPROVE_SQL = """
UPDATE crm.partner_commission SET status = 'approved', approved_at = now(), approved_by = $2
 WHERE partner_commission_id = $1
"""
UNAPPROVE_SQL = """
UPDATE crm.partner_commission SET status = 'accrued', approved_at = NULL, approved_by = NULL
 WHERE partner_commission_id = $1
"""
PAY_SQL = (
    "UPDATE crm.partner_commission SET status = 'paid', paid_on = $2, payout_ref = $3 WHERE partner_commission_id = $1"
)
CANCEL_SQL = """
UPDATE crm.partner_commission SET status = 'cancelled', cancel_reason = $2 WHERE partner_commission_id = $1
"""


def margin_role(ctx: TenantContext) -> bool:
    return decide(ctx.member_role or "", Action.VIEW_MARGIN).allowed


def money(value: Any) -> str | None:
    return None if value is None else f"{Decimal(value):.2f}"


def trip_ref(trip_id: uuid.UUID) -> str:
    """The short trip reference a partner sees (never the title, which may name a traveller)."""
    return "T-" + trip_id.hex[:8].upper()


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _text(raw: Any, field: str, *, max_len: int, required: bool = False) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise Invalid("invalid_partner", field, fields=[field])
        return None
    if not isinstance(raw, str) or len(raw.strip()) > max_len or _CONTROL.search(raw):
        raise Invalid("invalid_partner", field, fields=[field])
    return raw.strip()


def clean_terms(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """The commission terms of a create or an edit, merged over the current ones and checked as a whole (the database
    CHECK says the same: a percent at most 100, a currency exactly for a per-traveller rate)."""
    terms = {k: (current or {}).get(k) for k in ("commission_type", "rate", "currency", "basis", "payout_due_days")}
    terms = {
        "commission_type": terms["commission_type"] or "percent",
        "rate": terms["rate"] if terms["rate"] is not None else Decimal(0),
        "currency": terms["currency"],
        "basis": terms["basis"] or "revenue",
        "payout_due_days": terms["payout_due_days"] if terms["payout_due_days"] is not None else 30,
    }
    for key in terms:
        if key in body:
            terms[key] = body[key]
    if terms["commission_type"] not in TYPES:
        raise Invalid("invalid_partner", "commission_type", fields=["commission_type"])
    if terms["basis"] not in BASES:
        raise Invalid("invalid_partner", "basis", fields=["basis"])
    try:
        rate = Decimal(str(terms["rate"]))
    except ArithmeticError:
        raise Invalid("invalid_partner", "rate", fields=["rate"]) from None
    if not rate.is_finite() or rate < 0 or rate != rate.quantize(Decimal("0.01")):
        raise Invalid("invalid_partner", "rate", fields=["rate"])
    if terms["commission_type"] == "fixed_per_pax":
        if rate > Decimal("9999999.99"):
            raise Invalid("invalid_partner", "rate", fields=["rate"])
        currency = terms["currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise Invalid("invalid_partner", "a currency for a per-traveller rate", fields=["currency"])
    else:
        if rate > 100:
            raise Invalid("invalid_partner", "a percent is at most 100", fields=["rate"])
        terms["currency"] = None
    days = terms["payout_due_days"]
    if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 365:
        raise Invalid("invalid_partner", "payout_due_days", fields=["payout_due_days"])
    terms["rate"] = rate
    return terms


def _contact(body: dict[str, Any], field: str) -> str | None:
    value = _text(body.get(field), field, max_len=200)
    if value is None:
        return None
    if field == "contact_email" and (value.count("@") != 1 or " " in value or "." not in value.split("@")[1]):
        raise Invalid("invalid_partner", "an e-mail address", fields=[field])
    if field == "contact_phone" and not _PHONE.match(value):
        raise Invalid("invalid_partner", "a phone number", fields=[field])
    return value


async def _name(vault: Vault, partner_id: uuid.UUID, blob: bytes) -> str:
    return await vault.decrypt(TABLE, "name_enc", partner_id, blob)


def _terms_view(row: Any) -> dict[str, Any]:
    return {
        "commission_type": row["commission_type"],
        "rate": money(row["rate"]),
        "currency": row["currency"],
        "basis": row["basis"],
        "payout_due_days": row["payout_due_days"],
    }


async def list_partners(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    rows = await ctx.conn.fetch(PARTNERS_SQL)
    margin = margin_role(ctx)
    links: dict[uuid.UUID, dict[str, Any]] = {}
    if ctx.member_role == "owner":
        for link in await ctx.conn.fetch(LINKS_SQL):
            links.setdefault(link["sales_partner_id"], {})[link["purpose"]] = {
                "link_id": str(link["link_id"]),
                "intake_form_id": str(link["intake_form_id"]) if link["intake_form_id"] else None,
                "expires_at": _iso(link["expires_at"]),
                "last_used_at": _iso(link["last_used_at"]),
                "use_count": link["use_count"],
            }
    base = await ctx.conn.fetchval(BASE_SQL)
    totals: dict[uuid.UUID, dict[str, Any]] = {}
    if margin:
        for t in await ctx.conn.fetch(OPEN_TOTALS_SQL, base):
            totals.setdefault(t["sales_partner_id"], {})[t["status"]] = money(t["amount_base"] or 0)
    items = []
    for row in rows:
        item: dict[str, Any] = {
            "sales_partner_id": str(row["sales_partner_id"]),
            "kind": row["kind"],
            "name": await _name(vault, row["sales_partner_id"], row["name_enc"]),
            "status": row["status"],
            "has_email": row["has_email"],
            "has_phone": row["has_phone"],
            "trip_count": int(row["trip_count"]),
        }
        if margin:
            item.update(_terms_view(row))
            open_totals = totals.get(row["sales_partner_id"], {})
            item["accrued_base"] = open_totals.get("accrued", "0.00")
            item["approved_base"] = open_totals.get("approved", "0.00")
        if ctx.member_role == "owner":
            item["links"] = links.get(row["sales_partner_id"], {})
        items.append(item)
    ctx.audit("sales_partner_list", "allowed", "sales_partner", object_ids=tuple(i["sales_partner_id"] for i in items))
    return {"items": items, "base_currency": base, "terms_visible": margin}


async def _partner(ctx: TenantContext, partner_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(PARTNER_SQL, partner_id)
    if row is None:
        raise NotFound("partner_not_found")
    return row


async def get_partner(ctx: TenantContext, vault: Vault, partner_id: uuid.UUID) -> dict[str, Any]:
    """One partner for the owner's edit form: the contact is decrypted (an owner-only route, logged)."""
    row = await _partner(ctx, partner_id)
    out = {
        "sales_partner_id": str(partner_id),
        "kind": row["kind"],
        "name": await _name(vault, partner_id, row["name_enc"]),
        "contact_email": await vault.decrypt(TABLE, "contact_email_enc", partner_id, row["contact_email_enc"])
        if row["contact_email_enc"] is not None
        else None,
        "contact_phone": await vault.decrypt(TABLE, "contact_phone_enc", partner_id, row["contact_phone_enc"])
        if row["contact_phone_enc"] is not None
        else None,
        "status": row["status"],
        **_terms_view(row),
    }
    ctx.audit(
        "sales_partner_view", "allowed", "sales_partner", str(partner_id), fields=("contact_email", "contact_phone")
    )
    return out


async def create_partner(ctx: TenantContext, vault: Vault, body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind")
    if kind not in KINDS:
        raise Invalid("invalid_partner", "kind", fields=["kind"])
    name = _text(body.get("name"), "name", max_len=MAX_NAME, required=True)
    email = _contact(body, "contact_email")
    phone = _contact(body, "contact_phone")
    terms = clean_terms(body)
    partner_id = uuid.uuid4()
    await ctx.conn.execute(ENSURE_SETTINGS_SQL)
    await ctx.conn.execute(
        INSERT_SQL,
        partner_id,
        kind,
        await vault.encrypt(TABLE, "name_enc", partner_id, name),  # type: ignore[arg-type]
        await vault.encrypt(TABLE, "contact_email_enc", partner_id, email) if email else None,
        await vault.encrypt(TABLE, "contact_phone_enc", partner_id, phone) if phone else None,
        terms["commission_type"],
        terms["rate"],
        terms["currency"],
        terms["basis"],
        terms["payout_due_days"],
    )
    ctx.audit("sales_partner_create", "allowed", "sales_partner", str(partner_id))
    return await get_partner(ctx, vault, partner_id)


async def update_partner(
    ctx: TenantContext, vault: Vault, partner_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    row = await _partner(ctx, partner_id)
    sets: dict[str, Any] = {}
    if "kind" in changes:
        if changes["kind"] not in KINDS:
            raise Invalid("invalid_partner", "kind", fields=["kind"])
        sets["kind"] = changes["kind"]
    if "name" in changes:
        name = _text(changes["name"], "name", max_len=MAX_NAME, required=True)
        sets["name_enc"] = await vault.encrypt(TABLE, "name_enc", partner_id, name)  # type: ignore[arg-type]
    for field in ("contact_email", "contact_phone"):
        if field in changes:
            value = _contact(changes, field)
            sets[f"{field}_enc"] = await vault.encrypt(TABLE, f"{field}_enc", partner_id, value) if value else None
    if changes.keys() & {"commission_type", "rate", "currency", "basis", "payout_due_days"}:
        sets.update(clean_terms(changes, dict(row)))
    if "status" in changes:
        if changes["status"] not in ("active", "inactive"):
            raise Invalid("invalid_partner", "status", fields=["status"])
        if changes["status"] != row["status"]:
            sets["status"] = changes["status"]
    if sets:
        columns = list(sets)
        assignments = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(columns))
        await ctx.conn.execute(
            f"UPDATE crm.sales_partner SET {assignments} WHERE sales_partner_id = $1",  # noqa: S608 — fixed column names
            partner_id,
            *[sets[c] for c in columns],
        )
        ctx.audit("sales_partner_update", "allowed", "sales_partner", str(partner_id),
                  fields=tuple(c.removesuffix("_enc") for c in columns))  # fmt: skip
    return await get_partner(ctx, vault, partner_id)


async def delete_partner(ctx: TenantContext, partner_id: uuid.UUID) -> dict[str, Any]:
    await _partner(ctx, partner_id)
    if await ctx.conn.fetchval(IN_USE_SQL, partner_id):
        raise Conflict("partner_in_use", "a trip, a commission or a mail names this partner: deactivate it instead")
    await ctx.conn.execute(DELETE_SQL, partner_id)
    ctx.audit("sales_partner_delete", "allowed", "sales_partner", str(partner_id))
    return {"sales_partner_id": str(partner_id), "deleted": True}


def new_token() -> tuple[str, bytes]:
    """A 43-character link token and the SHA-256 the database keeps."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).digest()


async def issue_link(
    ctx: TenantContext, partner_id: uuid.UUID, purpose: str, *, intake_form_id: uuid.UUID | None, days: int | None
) -> dict[str, Any]:
    row = await _partner(ctx, partner_id)
    if row["status"] != "active":
        raise Conflict("partner_inactive", "activate the partner first")
    if purpose not in PURPOSES:
        raise Invalid("invalid_link", "purpose", fields=["purpose"])
    if purpose == "intake":
        if intake_form_id is None or not await ctx.conn.fetchval(FORM_SQL, intake_form_id):
            raise Invalid("invalid_link", "an active inquiry form", fields=["intake_form_id"])
    else:
        intake_form_id = None
        if row["contact_email_enc"] is None:
            raise Conflict("partner_no_email", "add the partner's e-mail first: the sign-in code goes there")
    days = DEFAULT_LINK_DAYS if days is None else days
    if not 1 <= days <= MAX_LINK_DAYS:
        raise Invalid("invalid_link", "days", fields=["days"])
    await ctx.conn.execute(REVOKE_ACTIVE_SQL, partner_id, purpose)
    token, token_hash = new_token()
    link_id = uuid.uuid4()
    expires = await ctx.conn.fetchval(INSERT_LINK_SQL, link_id, partner_id, purpose, intake_form_id, token_hash, days)
    ctx.audit("sales_partner_link", "allowed", "sales_partner_link", str(link_id), reason=purpose)
    return {
        "link_id": str(link_id),
        "purpose": purpose,
        "path": LINK_PATHS[purpose] + token,
        "expires_at": _iso(expires),
    }


async def revoke_link(ctx: TenantContext, partner_id: uuid.UUID, link_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchval(REVOKE_LINK_SQL, link_id, partner_id) is None:
        raise NotFound("link_not_found")
    ctx.audit("sales_partner_link_revoke", "allowed", "sales_partner_link", str(link_id))
    return {"link_id": str(link_id), "status": "revoked"}


async def set_trip_partner(ctx: TenantContext, trip_id: uuid.UUID, partner_id: uuid.UUID | None) -> dict[str, Any]:
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    await cancellation.ensure_not_cancelled(ctx, trip_id)
    if partner_id is not None and partner_id != trip["sales_partner_id"]:
        if not await ctx.conn.fetchval(ACTIVE_PARTNER_SQL, partner_id):
            raise Invalid("invalid_partner", "an active partner of the agency", fields=["sales_partner_id"])
    if partner_id != trip["sales_partner_id"]:
        await ctx.conn.execute(SET_TRIP_SQL, trip_id, partner_id)  # the trigger runs the commission sync
        ctx.audit("trip_sales_partner", "allowed", "trip", str(trip_id))
    return {"trip_id": str(trip_id), "sales_partner_id": str(partner_id) if partner_id else None}


def commission_view(row: Any) -> dict[str, Any]:
    return {
        "partner_commission_id": str(row["partner_commission_id"]),
        "trip_id": str(row["trip_id"]),
        "trip_ref": trip_ref(row["trip_id"]),
        "trip_stage": row["stage"],
        "start_date": _iso(row["start_date"]),
        "sales_partner_id": str(row["sales_partner_id"]),
        "commission_type": row["commission_type"],
        "rate": money(row["rate"]),
        "basis": row["basis"],
        "basis_amount": money(row["basis_amount"]),
        "pax": row["pax"],
        "amount": money(row["amount"]),
        "currency": row["currency"],
        "amount_base": money(row["amount_base"]),
        "base_currency": row["base_currency"],
        "computed_at": _iso(row["computed_at"]),
        "due_on": _iso(row["due_on"]),
        "overdue": row["status"] in OPEN and row["due_on"] is not None and row["due_on"] < dt.date.today(),
        "approved_at": _iso(row["approved_at"]),
        "paid_on": _iso(row["paid_on"]),
        "payout_ref": row["payout_ref"],
        "cancel_reason": row["cancel_reason"],
        "status": row["status"],
    }


def _status_filter(status: str | None) -> list[str] | None:
    if status in (None, "", "open"):
        return list(OPEN)
    if status == "all":
        return None
    if status not in STATUSES:
        raise Invalid("invalid_filter", "status", fields=["status"])
    return [status]


async def list_commissions(
    ctx: TenantContext, *, status: str | None, partner_id: uuid.UUID | None, trip_id: uuid.UUID | None
) -> dict[str, Any]:
    rows = await ctx.conn.fetch(COMMISSIONS_SQL, _status_filter(status), partner_id, trip_id)
    base = await ctx.conn.fetchval(BASE_SQL)
    totals: dict[str, Decimal] = {s: Decimal(0) for s in STATUSES}
    unconverted = 0
    for r in rows:
        if r["amount_base"] is not None and r["base_currency"] == base:
            totals[r["status"]] += r["amount_base"]
        else:
            unconverted += 1
    return {
        "items": [commission_view(r) for r in rows],
        "base_currency": base,
        "totals": {s: money(v) for s, v in totals.items()},
        "unconverted": unconverted,
    }


async def _commission(ctx: TenantContext, commission_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(COMMISSION_SQL, commission_id)
    if row is None:
        raise NotFound("commission_not_found")
    return row


async def _reread(ctx: TenantContext, commission_id: uuid.UUID) -> dict[str, Any]:
    return commission_view(await _commission(ctx, commission_id))


async def recalculate(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    """Refresh the trip's accrued partner commission (the sync; approved and paid rows stay)."""
    if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    raw = await ctx.conn.fetchval(SYNC_SQL, trip_id)
    counts = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    ctx.audit("partner_commission_sync", "allowed", "trip", str(trip_id))
    return {"trip_id": str(trip_id), "result": {k: int(v) for k, v in counts.items()}}


async def approve(ctx: TenantContext, commission_id: uuid.UUID) -> dict[str, Any]:
    row = await _commission(ctx, commission_id)
    if row["status"] != "accrued":
        raise Conflict("commission_not_accrued", "only an accrued commission is approved")
    # Approve what the trip says now: the accrued amount is refreshed first (the margin moves as bills arrive).
    await ctx.conn.fetchval(SYNC_SQL, row["trip_id"])
    row = await _commission(ctx, commission_id)
    if row["status"] != "accrued":
        raise Conflict("commission_changed", "the trip changed: this commission no longer applies")
    await ctx.conn.execute(APPROVE_SQL, commission_id, f"member:{ctx.member_id}")
    ctx.audit("partner_commission_approve", "allowed", "partner_commission", str(commission_id))
    return await _reread(ctx, commission_id)


async def unapprove(ctx: TenantContext, commission_id: uuid.UUID) -> dict[str, Any]:
    row = await _commission(ctx, commission_id)
    if row["status"] != "approved":
        raise Conflict("commission_not_approved", "only an approved commission goes back")
    await ctx.conn.execute(UNAPPROVE_SQL, commission_id)
    await ctx.conn.fetchval(SYNC_SQL, row["trip_id"])  # accrued again: follows the trip from now on
    ctx.audit("partner_commission_unapprove", "allowed", "partner_commission", str(commission_id))
    return await _reread(ctx, commission_id)


async def pay(
    ctx: TenantContext, commission_id: uuid.UUID, *, paid_on: dt.date | None, reference: str | None
) -> dict[str, Any]:
    row = await _commission(ctx, commission_id)
    if row["status"] != "approved":
        raise Conflict("commission_not_approved", "approve the commission before marking it paid")
    paid_on = paid_on or dt.date.today()
    if paid_on > dt.date.today():
        raise Invalid("invalid_payout", "paid_on is not in the future", fields=["paid_on"])
    if reference is not None:
        reference = reference.strip() or None
        if reference is not None and not _REF.match(reference):
            raise Invalid("invalid_payout", "reference", fields=["reference"])
    await ctx.conn.execute(PAY_SQL, commission_id, paid_on, reference)
    ctx.audit("partner_commission_pay", "allowed", "partner_commission", str(commission_id))
    return await _reread(ctx, commission_id)


async def cancel(ctx: TenantContext, commission_id: uuid.UUID) -> dict[str, Any]:
    row = await _commission(ctx, commission_id)
    if row["status"] not in OPEN:
        raise Conflict("commission_closed", "a paid or cancelled commission stays as it is")
    await ctx.conn.execute(CANCEL_SQL, commission_id, "by_owner")
    ctx.audit("partner_commission_cancel", "allowed", "partner_commission", str(commission_id))
    return await _reread(ctx, commission_id)


STATEMENT_COLUMNS = (
    "trip_ref", "start_date", "pax", "commission_type", "rate", "basis", "basis_amount", "amount", "currency",
    "status", "due_on", "approved_at", "paid_on", "payout_ref",
)  # fmt: skip


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # A cell a spreadsheet would run as a formula starts with a quote (the reference is the agency's own text).
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def statement_csv(ctx: TenantContext, partner_id: uuid.UUID, *, status: str | None) -> tuple[str, str]:
    """(file name, CSV text) of one partner's commissions: no traveller name, the trip by its short reference."""
    await _partner(ctx, partner_id)
    rows = await ctx.conn.fetch(COMMISSIONS_SQL, _status_filter(status or "all"), partner_id, None)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(STATEMENT_COLUMNS)
    for r in rows:
        view = commission_view(r)
        view["pax"] = r["pax"]
        writer.writerow([_csv_cell(view.get(c)) for c in STATEMENT_COLUMNS])
    ctx.audit("sales_partner_statement", "allowed", "sales_partner", str(partner_id))
    return f"partner-statement-{partner_id.hex[:8]}-{dt.date.today().isoformat()}.csv", buf.getvalue()
