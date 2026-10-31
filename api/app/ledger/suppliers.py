"""The agency's supplier directory (design v3 M15, A-13; crm-014 ``crm.agency_supplier``).

- Adventure Asia is a built-in row (``kind = aa``) the database creates (``crm.ensure_aa_supplier()``): the list makes
  sure it exists, provisioning creates it with the agency. Nobody changes or deletes it (CRM22 -> 409
  ``aa_supplier_builtin``).
- The agency's own suppliers: name, category, default commission (a margin key: the serializer strips
  ``default_commission_pct`` for the assistant), ``confirmation_unique``; contact name and e-mail are vault-encrypted
  (AAD = tenant, ``agency_supplier``, column, row id). Lists never decrypt them: they say which are filled, and one
  ``list_view`` event per load names the suppliers shown. A contact field opens through :func:`reveal_contact` with a
  reason code (one ``reveal`` event), the F1 reveal pattern (AA-937).
- Delete moves an own supplier to the trash (``app.lifecycle.trash``), refused while live lines name it (409
  ``supplier_in_use``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.ledger import refusal
from app.tenancy import AnyContext, TenantContext
from app.travellers.fields import REVEAL_REASONS
from app.vault import Vault

TABLE = "agency_supplier"
CATEGORIES = (
    "accommodation",
    "transport",
    "activity",
    "guide",
    "restaurant",
    "flight",
    "insurance",
    "visa",
    "dmc",
    "other",
)
CONTACT_FIELDS = {"contact_name": "contact_name_enc", "contact_email": "contact_email_enc"}
ENSURE_AA_SQL = "SELECT crm.ensure_aa_supplier()"
LIST_SQL = """
SELECT agency_supplier_id, kind, name, category, default_commission_pct, confirmation_unique, commission_due_days,
       contact_name_enc IS NOT NULL AS has_contact_name, contact_email_enc IS NOT NULL AS has_contact_email,
       (SELECT count(*) FROM crm.booking_line l
         WHERE l.agency_supplier_id = s.agency_supplier_id AND l.deleted_at IS NULL)::int AS line_count
  FROM crm.agency_supplier s
 WHERE deleted_at IS NULL
 ORDER BY kind = 'aa' DESC, lower(name), agency_supplier_id
"""
ONE_SQL = LIST_SQL.replace("WHERE deleted_at IS NULL", "WHERE deleted_at IS NULL AND agency_supplier_id = $1")
ROW_SQL = """
SELECT kind, deleted_at, contact_name_enc, contact_email_enc FROM crm.agency_supplier WHERE agency_supplier_id = $1
"""
INSERT_SQL = """
INSERT INTO crm.agency_supplier (agency_supplier_id, name, category, contact_name_enc, contact_email_enc,
                                 default_commission_pct, confirmation_unique, commission_due_days)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""
LIVE_LINES_SQL = "SELECT count(*) FROM crm.booking_line WHERE agency_supplier_id = $1 AND deleted_at IS NULL"
EDITABLE = (
    "name",
    "category",
    "default_commission_pct",
    "commission_due_days",
    "confirmation_unique",
    "contact_name",
    "contact_email",
)


def _name(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 or any(ord(c) < 32 for c in value):
        raise Invalid("invalid_name", "1 to 120 characters")
    return value.strip()


def _category(value: Any) -> str:
    if value not in CATEGORIES:
        raise Invalid("invalid_category", ", ".join(CATEGORIES))
    return str(value)


def _pct(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        pct = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid("invalid_default_commission_pct", "a percentage from 0 to 100") from None
    if not pct.is_finite() or not Decimal(0) <= pct <= Decimal(100):
        raise Invalid("invalid_default_commission_pct", "a percentage from 0 to 100")
    return pct.quantize(Decimal("0.01"))


def _due_days(value: Any) -> int | None:
    """AA-957: days after the service the supplier pays its commission (None = the 30-day default)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 365:
        raise Invalid("invalid_commission_due_days", "0 to 365 days")
    return value


def _contact(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise Invalid(f"invalid_{field}", "at most 200 characters")
    if field == "contact_email" and ("@" not in value or " " in value.strip()):
        raise Invalid("invalid_contact_email", "an e-mail address")
    return value.strip()


def _view(row: Any) -> dict[str, Any]:
    pct = row["default_commission_pct"]
    return {
        "agency_supplier_id": str(row["agency_supplier_id"]),
        "kind": row["kind"],
        "builtin": row["kind"] == "aa",
        "name": row["name"],
        "category": row["category"],
        "default_commission_pct": str(pct) if pct is not None else None,
        "commission_due_days": row["commission_due_days"],
        "confirmation_unique": row["confirmation_unique"],
        "has_contact_name": row["has_contact_name"],
        "has_contact_email": row["has_contact_email"],
        "line_count": row["line_count"],
    }


async def ensure_aa(ctx: AnyContext) -> uuid.UUID:
    """The agency's Adventure Asia row, created when missing (the provisioning path and the supplier list)."""
    supplier_id = await ctx.conn.fetchval(ENSURE_AA_SQL)
    assert isinstance(supplier_id, uuid.UUID)  # noqa: S101 — the function returns the row id
    return supplier_id


async def list_suppliers(ctx: TenantContext) -> dict[str, Any]:
    await ensure_aa(ctx)
    rows = await ctx.conn.fetch(LIST_SQL)
    ids = tuple(str(r["agency_supplier_id"]) for r in rows)
    ctx.audit("list_view", "allowed", TABLE, None, reason="supplier_list", object_ids=ids)
    return {"suppliers": [_view(r) for r in rows], "categories": list(CATEGORIES)}


async def get_supplier(ctx: TenantContext, supplier_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, supplier_id)
    if row is None:
        raise NotFound("supplier_not_found")
    ctx.audit("view", "allowed", TABLE, str(supplier_id))
    return _view(row)


async def create_supplier(ctx: TenantContext, vault: Vault, body: dict[str, Any]) -> dict[str, Any]:
    supplier_id = uuid.uuid4()
    name = _name(body.get("name"))
    category = _category(body.get("category", "other"))
    sealed: dict[str, bytes | None] = {}
    for field, column in CONTACT_FIELDS.items():
        value = _contact(body.get(field), field)
        sealed[column] = await vault.encrypt(TABLE, column, supplier_id, value) if value is not None else None
    await ctx.conn.execute(
        INSERT_SQL,
        supplier_id,
        name,
        category,
        sealed["contact_name_enc"],
        sealed["contact_email_enc"],
        _pct(body.get("default_commission_pct")),
        bool(body.get("confirmation_unique", False)),
        _due_days(body.get("commission_due_days")),
    )
    ctx.audit("create", "allowed", TABLE, str(supplier_id))
    return await get_supplier(ctx, supplier_id)


async def update_supplier(
    ctx: TenantContext, vault: Vault, supplier_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ROW_SQL, supplier_id)
    if row is None or row["deleted_at"] is not None:
        raise NotFound("supplier_not_found")
    if row["kind"] == "aa":
        raise Conflict("aa_supplier_builtin", "Adventure Asia is built in and cannot be changed")
    sets: list[str] = []
    args: list[Any] = [supplier_id]
    for field in EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "name":
            value, column = _name(value), "name"
        elif field == "category":
            value, column = _category(value), "category"
        elif field == "default_commission_pct":
            value, column = _pct(value), "default_commission_pct"
        elif field == "commission_due_days":
            value, column = _due_days(value), "commission_due_days"
        elif field == "confirmation_unique":
            if not isinstance(value, bool):
                raise Invalid("invalid_confirmation_unique", "true or false")
            column = "confirmation_unique"
        else:
            column = CONTACT_FIELDS[field]
            text = _contact(value, field)
            value = await vault.encrypt(TABLE, column, supplier_id, text) if text is not None else None
        args.append(value)
        sets.append(f"{column} = ${len(args)}")
    if sets:
        try:
            async with ctx.conn.transaction():
                await ctx.conn.execute(
                    f"UPDATE crm.agency_supplier SET {', '.join(sets)} WHERE agency_supplier_id = $1",  # noqa: S608
                    *args,
                )
        except asyncpg.PostgresError as exc:
            mapped = refusal(exc)
            if mapped is None:
                raise
            raise mapped from None
        fields = tuple(sorted(f for f in changes if f in EDITABLE))
        ctx.audit("update", "allowed", TABLE, str(supplier_id), fields=fields)
    return await get_supplier(ctx, supplier_id)


async def reveal_contact(
    ctx: TenantContext, vault: Vault, supplier_id: uuid.UUID, field: str, reason: str
) -> dict[str, Any]:
    if field not in CONTACT_FIELDS:
        raise Invalid("invalid_field", ", ".join(CONTACT_FIELDS))
    if reason not in REVEAL_REASONS:
        raise Invalid("reason_required", "pick why you open this field", reasons=list(REVEAL_REASONS))
    row = await ctx.conn.fetchrow(ROW_SQL, supplier_id)
    if row is None or row["deleted_at"] is not None:
        raise NotFound("supplier_not_found")
    column = CONTACT_FIELDS[field]
    blob = row[column]
    value = await vault.decrypt(TABLE, column, supplier_id, blob) if blob is not None else None
    ctx.audit("reveal", "allowed", TABLE, str(supplier_id), fields=(field,), reason=reason)
    return {"agency_supplier_id": str(supplier_id), "field": field, "value": value}


async def trash_check(ctx: TenantContext, supplier_id: uuid.UUID) -> None:
    """Before the trash: the AA row never goes, nor a supplier that live lines name (the database refuses both too)."""
    row = await ctx.conn.fetchrow(ROW_SQL, supplier_id)
    if row is None or row["deleted_at"] is not None:
        raise NotFound("agency_supplier_not_found")
    if row["kind"] == "aa":
        raise Conflict("aa_supplier_builtin", "Adventure Asia is built in and cannot be deleted")
    if await ctx.conn.fetchval(LIVE_LINES_SQL, supplier_id):
        raise Conflict("supplier_in_use", "booking lines name this supplier; move or delete them first")
