"""The agency's traveller cancellation policy and the charge of a trip cancellation (AA-1067, items crm-11 / crm-12;
crm-049, crm-050). Routes in :mod:`app.routers.cancel_policy`; the pure rules in :mod:`app.cancel_policy.rules`.

- **Settings > Cancellation policy** (owner, ``PAYMENT_SETTINGS``; every role reads): drafts (the AA starting table, a
  blank one, or a new version of a published one), tier edits on a draft, delete a draft, **publish** (frozen from
  then on: the database guard CRM49; publishing a new version retires the one it replaces and takes over its default
  flag), set the default, retire.
- **Pinned on a proposal** when it goes out (:func:`pin`: builder publish, text send): the agency default unless the
  version names one; the price hash covers it and the version seal (CRM31) keeps it. The client reads it on the
  proposal page and in the booking terms (merge field ``{{cancellation.policy}}``, :func:`merge_text`).
- **Cancel dialog** (:func:`assess`): days before departure (the agency's today), the frozen policy (else the current
  default, flagged ``policy_not_frozen``), its tier and charge, the money paid, the decision (policy / override with a
  reason / waive with a reason), the refund due and its split per payment (:func:`app.cancel_policy.rules.allocate`),
  the fee invoice amount. An agent may lower the policy charge (override or waive) by at most the agency's refund
  threshold (``tenant_settings.refund_owner_threshold_base``, in the base currency); more is the owner's call (403
  ``cancel_charge_owner_only``; no rate for the day = the owner, fail closed).
- **Record** (:func:`record`): the cancel run writes one ``crm.cancellation_charge`` row (the trip P&L seam of crm-30:
  ``charge_amount`` is the fee; AA's cost stays the booking line money ``cancellation_cost``).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import asyncpg

from app.cancel_policy import rules
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.tenancy import AnyContext, TenantContext

MAX_POLICIES = 50
CENT = Decimal("0.01")

LIST_SQL = """
SELECT policy_id, name, version, replaces_policy_id, currency, is_default, published_at, retired_at, created_at,
       updated_at
  FROM crm.cancellation_policy WHERE deleted_at IS NULL
 ORDER BY retired_at IS NOT NULL, published_at IS NULL, name, version DESC
 LIMIT $1
"""
ONE_SQL = """
SELECT policy_id, name, version, replaces_policy_id, currency, is_default, published_at, retired_at, created_at,
       updated_at
  FROM crm.cancellation_policy WHERE policy_id = $1 AND deleted_at IS NULL
"""
LOCK_SQL = ONE_SQL.rstrip() + "\n   FOR UPDATE"
TIERS_SQL = """
SELECT policy_id, days_before_min, charge_pct, min_charge_amount FROM crm.cancellation_policy_tier
 WHERE policy_id = ANY($1::uuid[]) AND deleted_at IS NULL
 ORDER BY days_before_min DESC
"""
DEFAULT_SQL = """
SELECT policy_id, name, version, replaces_policy_id, currency, is_default, published_at, retired_at, created_at,
       updated_at
  FROM crm.cancellation_policy WHERE is_default AND deleted_at IS NULL
"""
SETTINGS_ROW_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
COUNT_SQL = "SELECT count(*) FROM crm.cancellation_policy WHERE deleted_at IS NULL"
INSERT_SQL = """
INSERT INTO crm.cancellation_policy (policy_id, name, version, replaces_policy_id, currency)
VALUES ($1, $2, $3, $4, $5)
"""
INSERT_TIER_SQL = """
INSERT INTO crm.cancellation_policy_tier (policy_id, days_before_min, charge_pct, min_charge_amount)
VALUES ($1, $2, $3, $4)
"""
DELETE_TIERS_SQL = "DELETE FROM crm.cancellation_policy_tier WHERE policy_id = $1"
UPDATE_DRAFT_SQL = "UPDATE crm.cancellation_policy SET name = $2, currency = $3 WHERE policy_id = $1"
DELETE_SQL = "DELETE FROM crm.cancellation_policy WHERE policy_id = $1"
DRAFT_OF_SQL = """
SELECT policy_id FROM crm.cancellation_policy
 WHERE replaces_policy_id = $1 AND published_at IS NULL AND deleted_at IS NULL
"""
CLEAR_DEFAULT_SQL = "UPDATE crm.cancellation_policy SET is_default = false WHERE is_default AND policy_id <> $1"
PUBLISH_SQL = "UPDATE crm.cancellation_policy SET published_at = now() WHERE policy_id = $1"
SET_DEFAULT_SQL = "UPDATE crm.cancellation_policy SET is_default = true WHERE policy_id = $1"
RETIRE_SQL = """
UPDATE crm.cancellation_policy SET retired_at = now(), is_default = false WHERE policy_id = $1 AND retired_at IS NULL
"""
PIN_SQL = """
UPDATE crm.proposal SET cancellation_policy_id = $2 WHERE proposal_id = $1 AND cancellation_policy_id IS NULL
"""
TRIP_FACTS_SQL = """
SELECT t.start_date,
       p.proposal_id, p.cancellation_policy_id, p.currency, p.kind,
       coalesce(p.accepted_total_amount, p.total_amount) AS total
  FROM crm.trip t
  LEFT JOIN crm.proposal p ON p.trip_id = t.trip_id AND p.status = 'accepted' AND p.deleted_at IS NULL
 WHERE t.trip_id = $1 AND t.deleted_at IS NULL
"""
TODAY_SQL = """
SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), 'Asia/Ho_Chi_Minh'))::date
"""
THRESHOLD_SQL = "SELECT refund_owner_threshold_base FROM crm.tenant_settings"
INSERT_CHARGE_SQL = """
INSERT INTO crm.cancellation_charge (trip_id, proposal_id, policy_id, currency, trip_total, days_before, charge_pct,
                                     policy_charge, charge_amount, decision, reason, paid_amount, kept_amount,
                                     refund_due, invoiced_amount, invoice_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
RETURNING charge_id
"""
CHARGE_SQL = """
SELECT charge_id, trip_id, proposal_id, policy_id, currency, trip_total, days_before, charge_pct, policy_charge,
       charge_amount, decision, reason, paid_amount, kept_amount, refund_due, invoiced_amount, invoice_id, created_at,
       created_by
  FROM crm.cancellation_charge WHERE trip_id = $1 AND deleted_at IS NULL
"""


def _money(value: Any) -> str | None:
    return None if value is None else str(Decimal(value).quantize(CENT))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _name(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text or len(text) > rules.MAX_NAME or any(ord(c) < 32 for c in text):
        raise Invalid("invalid_name", f"1 to {rules.MAX_NAME} characters, one line", field="name")
    return text


def _currency(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not (isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isupper()):
        raise Invalid("invalid_currency", "a three-letter code (ISO 4217)", field="currency")
    return value


def _tiers_of(rows: list[Any]) -> list[rules.Tier]:
    return [
        rules.Tier(
            int(r["days_before_min"]),
            Decimal(r["charge_pct"]),
            None if r["min_charge_amount"] is None else Decimal(r["min_charge_amount"]),
        )
        for r in rows
    ]


def _status(row: Any) -> str:
    if row["retired_at"] is not None:
        return "retired"
    return "published" if row["published_at"] is not None else "draft"


def _view(row: Any, tiers: list[rules.Tier]) -> dict[str, Any]:
    return {
        "policy_id": str(row["policy_id"]),
        "name": row["name"],
        "version": row["version"],
        "status": _status(row),
        "replaces_policy_id": str(row["replaces_policy_id"]) if row["replaces_policy_id"] else None,
        "currency": row["currency"],
        "is_default": row["is_default"],
        "published_at": _iso(row["published_at"]),
        "retired_at": _iso(row["retired_at"]),
        "updated_at": _iso(row["updated_at"]),
        "tiers": rules.tiers_view(tiers),
        "text": {
            locale: rules.policy_text(row["name"], row["version"], tiers, row["currency"], locale)
            for locale in ("en", "vi")
        }
        if tiers
        else None,
    }


async def _tiers(ctx: AnyContext, policy_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[rules.Tier]]:
    out: dict[uuid.UUID, list[Any]] = {pid: [] for pid in policy_ids}
    for r in await ctx.conn.fetch(TIERS_SQL, policy_ids):
        out.setdefault(r["policy_id"], []).append(r)
    return {pid: _tiers_of(rows) for pid, rows in out.items()}


async def _one(ctx: AnyContext, policy_id: uuid.UUID, *, lock: bool = False) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL if lock else ONE_SQL, policy_id)
    if row is None:
        raise NotFound("policy_not_found")
    return row


async def get(ctx: AnyContext, policy_id: uuid.UUID) -> dict[str, Any]:
    row = await _one(ctx, policy_id)
    return _view(row, (await _tiers(ctx, [policy_id]))[policy_id])


async def listing(ctx: TenantContext) -> dict[str, Any]:
    """Every version of the agency's policies (current first, drafts, then retired) and the AA starting table."""
    rows = await ctx.conn.fetch(LIST_SQL, MAX_POLICIES)
    tiers = await _tiers(ctx, [r["policy_id"] for r in rows])
    ctx.audit("list_view", "allowed", "cancellation_policy", None,
              object_ids=tuple(str(r["policy_id"]) for r in rows))  # fmt: skip
    return {
        "policies": [_view(r, tiers[r["policy_id"]]) for r in rows],
        "aa_default": {"name": rules.DEFAULT_NAME, "tiers": rules.tiers_view(rules.default_tiers())},
    }


async def create(ctx: TenantContext, body: dict[str, Any]) -> dict[str, Any]:
    """A draft: ``from_policy_id`` = a new version of that published policy (its name, currency and tiers);
    ``aa_default`` = the AA starting table; else the ``name`` / ``currency`` / ``tiers`` given."""
    await ctx.conn.execute(SETTINGS_ROW_SQL)
    if int(await ctx.conn.fetchval(COUNT_SQL)) >= MAX_POLICIES:
        raise Conflict("too_many_policies", f"at most {MAX_POLICIES} policy versions; delete drafts first")
    policy_id = uuid.uuid4()
    source_id = body.get("from_policy_id")
    if source_id is not None:
        src = await _one(ctx, uuid.UUID(str(source_id)), lock=True)
        if src["published_at"] is None:
            raise Conflict("policy_is_draft", "edit the draft itself")
        if await ctx.conn.fetchval(DRAFT_OF_SQL, src["policy_id"]) is not None:
            raise Conflict("policy_draft_exists", "a new version of this policy is being drafted already")
        name, currency, version = src["name"], src["currency"], int(src["version"]) + 1
        tiers = (await _tiers(ctx, [src["policy_id"]]))[src["policy_id"]]
        replaces = src["policy_id"]
    elif body.get("aa_default"):
        name, currency, version, replaces = _name(body.get("name") or rules.DEFAULT_NAME), None, 1, None
        tiers = rules.default_tiers()
    else:
        name, currency, version, replaces = _name(body.get("name")), _currency(body.get("currency")), 1, None
        tiers = rules.check_tiers(body.get("tiers"))
    _min_needs_currency(tiers, currency)
    await ctx.conn.execute(INSERT_SQL, policy_id, name, version, replaces, currency)
    await _write_tiers(ctx, policy_id, tiers)
    ctx.audit("create", "allowed", "cancellation_policy", str(policy_id),
              reason="new_version" if replaces else ("aa_default" if body.get("aa_default") else "blank"))  # fmt: skip
    return await get(ctx, policy_id)


def _min_needs_currency(tiers: list[rules.Tier], currency: str | None) -> None:
    if currency is None and any(t.min_charge_amount is not None for t in tiers):
        raise Invalid("currency_required", "a minimum charge needs the policy currency", field="currency")


async def _write_tiers(ctx: AnyContext, policy_id: uuid.UUID, tiers: list[rules.Tier]) -> None:
    await ctx.conn.execute(DELETE_TIERS_SQL, policy_id)
    for t in tiers:
        await ctx.conn.execute(INSERT_TIER_SQL, policy_id, t.days_before_min, t.charge_pct, t.min_charge_amount)


async def update_draft(ctx: TenantContext, policy_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await _one(ctx, policy_id, lock=True)
    if row["published_at"] is not None:
        raise Conflict("policy_published", "a published policy never changes: make a new version")
    name = _name(changes["name"]) if "name" in changes else row["name"]
    currency = _currency(changes["currency"]) if "currency" in changes else row["currency"]
    tiers = rules.check_tiers(changes["tiers"]) if "tiers" in changes else (await _tiers(ctx, [policy_id]))[policy_id]
    _min_needs_currency(tiers, currency)
    await ctx.conn.execute(UPDATE_DRAFT_SQL, policy_id, name, currency)
    if "tiers" in changes:
        await _write_tiers(ctx, policy_id, tiers)
    ctx.audit("update", "allowed", "cancellation_policy", str(policy_id), fields=tuple(sorted(changes)))
    return await get(ctx, policy_id)


async def delete_draft(ctx: TenantContext, policy_id: uuid.UUID) -> dict[str, Any]:
    row = await _one(ctx, policy_id, lock=True)
    if row["published_at"] is not None:
        raise Conflict("policy_published", "a published policy is retired, never deleted")
    await ctx.conn.execute(DELETE_SQL, policy_id)
    ctx.audit("delete", "allowed", "cancellation_policy", str(policy_id))
    return {"policy_id": str(policy_id), "deleted": True}


async def publish(ctx: TenantContext, policy_id: uuid.UUID, *, make_default: bool) -> dict[str, Any]:
    """Freeze a draft. A new version retires the one it replaces and takes over its default flag; the agency's first
    published policy becomes the default."""
    row = await _one(ctx, policy_id, lock=True)
    if row["published_at"] is not None:
        raise Conflict("policy_published", "published already")
    if not (await _tiers(ctx, [policy_id]))[policy_id]:
        raise Conflict("policy_without_tiers", "add the charge table first")
    await ctx.conn.execute(PUBLISH_SQL, policy_id)
    default = await ctx.conn.fetchrow(DEFAULT_SQL)
    if row["replaces_policy_id"] is not None:
        old = await ctx.conn.fetchrow(LOCK_SQL, row["replaces_policy_id"])
        make_default = make_default or bool(old is not None and old["is_default"])
        if old is not None:
            await ctx.conn.execute(RETIRE_SQL, old["policy_id"])
    if make_default or default is None:
        await _make_default(ctx, policy_id)
    ctx.audit("publish", "allowed", "cancellation_policy", str(policy_id))
    return await get(ctx, policy_id)


async def _make_default(ctx: AnyContext, policy_id: uuid.UUID) -> None:
    await ctx.conn.execute(CLEAR_DEFAULT_SQL, policy_id)
    await ctx.conn.execute(SET_DEFAULT_SQL, policy_id)


async def set_default(ctx: TenantContext, policy_id: uuid.UUID) -> dict[str, Any]:
    row = await _one(ctx, policy_id, lock=True)
    if row["published_at"] is None or row["retired_at"] is not None:
        raise Conflict("policy_not_live", "only a published, current policy is the default")
    await _make_default(ctx, policy_id)
    ctx.audit("update", "allowed", "cancellation_policy", str(policy_id), fields=("is_default",))
    return await get(ctx, policy_id)


async def retire(ctx: TenantContext, policy_id: uuid.UUID) -> dict[str, Any]:
    """A retired version stays on the proposals that froze it; new proposals no longer get it."""
    row = await _one(ctx, policy_id, lock=True)
    if row["published_at"] is None:
        raise Conflict("policy_is_draft", "delete a draft instead")
    await ctx.conn.execute(RETIRE_SQL, policy_id)
    ctx.audit("update", "allowed", "cancellation_policy", str(policy_id), fields=("retired_at",))
    return await get(ctx, policy_id)


async def current(ctx: AnyContext) -> Any:
    """The agency's default published policy (None: the agency has none)."""
    return await ctx.conn.fetchrow(DEFAULT_SQL)


async def check_choice(ctx: AnyContext, policy_id: uuid.UUID | None) -> uuid.UUID | None:
    """A version the agent names on a draft proposal: published and current (409 ``policy_not_live``)."""
    if policy_id is None:
        return None
    row = await _one(ctx, policy_id)
    if row["published_at"] is None or row["retired_at"] is not None:
        raise Conflict("policy_not_live", "choose a published, current cancellation policy")
    return policy_id


async def pin(ctx: AnyContext, row: Any) -> Any:
    """A version going out (builder publish, text send) without a policy takes the agency default before its price
    hash is fixed (the pin_current_terms pattern). Returns the row with ``cancellation_policy_id`` (a dict copy when it
    changed)."""
    if row.get("cancellation_policy_id") is not None:
        return row
    default = await current(ctx)
    if default is None:
        return row
    await ctx.conn.execute(PIN_SQL, row["proposal_id"], default["policy_id"])
    ctx.audit("proposal_policy_pin", "allowed", "proposal", str(row["proposal_id"]), reason="agency_default")
    return {**dict(row), "cancellation_policy_id": default["policy_id"]}


async def summary(ctx: AnyContext, policy_id: uuid.UUID | None) -> dict[str, Any] | None:
    """What a client page shows of a frozen policy: name, version, tiers and the text in both languages."""
    if policy_id is None:
        return None
    row = await ctx.conn.fetchrow(ONE_SQL, policy_id)
    if row is None:
        return None
    view = _view(row, (await _tiers(ctx, [policy_id]))[policy_id])
    return {k: view[k] for k in ("policy_id", "name", "version", "currency", "tiers", "text")}


async def merge_text(ctx: AnyContext, policy_id: uuid.UUID | None, locale: str = "en") -> str | None:
    """The ``{{cancellation.policy}}`` value of a booking terms text (None = the agency has no policy on it)."""
    found = await summary(ctx, policy_id)
    if found is None or not found["text"]:
        return None
    return str(found["text"].get(locale) or found["text"]["en"])


# ── The cancel dialog ────────────────────────────────────────────────────────────────────────────


@dataclass
class Assessment:
    """What a cancellation charges (the dialog's ``charge`` block, then the run's record)."""

    currency: str | None
    total: Decimal
    days_before: int | None
    policy: Any
    tier: rules.Tier | None
    policy_charge: Decimal | None
    decision: str
    charge: Decimal
    reason: str | None
    paid: Decimal
    refunds: dict[str, Decimal]
    proposal_id: uuid.UUID | None
    frozen: bool
    warnings: list[str] = field(default_factory=list)
    needs_owner: bool = False

    @property
    def kept(self) -> Decimal:
        return (self.paid - sum(self.refunds.values(), Decimal(0))).quantize(CENT)

    @property
    def refund_due(self) -> Decimal:
        return rules.refund_due(self.paid, self.charge)

    @property
    def invoice_due(self) -> Decimal:
        return rules.invoice_due(self.charge, self.kept)

    def view(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "trip_total": _money(self.total),
            "days_before": self.days_before,
            "policy": None
            if self.policy is None
            else {
                "policy_id": str(self.policy["policy_id"]),
                "name": self.policy["name"],
                "version": self.policy["version"],
                "frozen": self.frozen,
            },  # fmt: skip
            "tier": None
            if self.tier is None
            else {"days_before_min": self.tier.days_before_min, "charge_pct": str(self.tier.charge_pct)},
            "policy_charge": _money(self.policy_charge),
            "decision": self.decision,
            "charge_amount": _money(self.charge),
            "reason": self.reason,
            "paid_amount": _money(self.paid),
            "kept_amount": _money(self.kept),
            "refund_due": _money(self.refund_due),
            "invoice_amount": _money(self.invoice_due),
            "warnings": sorted(set(self.warnings)),
            "needs_owner": self.needs_owner,
        }


async def assess(
    ctx: TenantContext,
    trip_id: uuid.UUID,
    payments: list[dict[str, Any]],
    decision: rules.Decision,
) -> Assessment:
    """The charge of cancelling ``trip_id`` now. ``payments``: the cancel plan's payments (oldest first) with their
    ``refundable`` amount and currency; the charge is kept from those in the trip currency."""
    facts = await ctx.conn.fetchrow(TRIP_FACTS_SQL, trip_id)
    if facts is None:
        raise NotFound("trip_not_found")
    today = await ctx.conn.fetchval(TODAY_SQL)
    days = rules.days_before(facts["start_date"], today)
    warnings: list[str] = []
    if facts["start_date"] is None:
        warnings.append("no_start_date")
    currency = facts["currency"]
    total = Decimal(facts["total"] or 0).quantize(CENT)
    policy, frozen = None, False
    if facts["cancellation_policy_id"] is not None:
        policy, frozen = await ctx.conn.fetchrow(ONE_SQL, facts["cancellation_policy_id"]), True
    elif facts["proposal_id"] is not None:
        policy = await current(ctx)
        if policy is not None:
            warnings.append("policy_not_frozen")  # accepted before the agency had a policy: the current one applies
    tier, amount = None, None
    if policy is not None and currency is not None:
        tiers = (await _tiers(ctx, [policy["policy_id"]]))[policy["policy_id"]]
        if tiers:
            tier, amount = rules.policy_charge(total, days, tiers, currency, policy["currency"])
            if tier.min_charge_amount is not None and policy["currency"] != currency:
                warnings.append("min_charge_other_currency")
    if facts["proposal_id"] is None:
        warnings.append("no_accepted_proposal")
    code, charge = rules.final_charge(amount if tier is not None else None, decision, total)
    same = [p for p in payments if currency is not None and p["currency"] == currency]
    if len(same) < len(payments):
        warnings.append("payments_other_currency")
    paid = sum((Decimal(p["refundable"]) for p in same), Decimal(0)).quantize(CENT)
    refunds = rules.allocate([(p["payment_id"], Decimal(p["refundable"])) for p in same], charge)
    out = Assessment(currency, total, days, policy, tier, amount, code, charge, decision.reason, paid, refunds,
                     facts["proposal_id"], frozen, warnings)  # fmt: skip
    out.needs_owner = await _needs_owner(ctx, out)
    return out


async def _needs_owner(ctx: TenantContext, a: Assessment) -> bool:
    """An agent lowers the policy charge by at most the refund threshold (base currency); else the owner decides."""
    if ctx.member_role == "owner" or a.decision not in ("override", "waived"):
        return False
    lowered = (a.policy_charge or Decimal(0)) - a.charge
    if lowered <= 0 or a.currency is None:
        return False
    from app.payments import service as payments

    _base, in_base = await payments.base_amount(ctx, lowered, a.currency)
    threshold = await ctx.conn.fetchval(THRESHOLD_SQL)
    threshold = payments.DEFAULT_THRESHOLD if threshold is None else Decimal(threshold)
    return in_base is None or in_base > threshold


def require_allowed(ctx: TenantContext, a: Assessment, trip_id: uuid.UUID) -> None:
    if a.needs_owner:
        ctx.audit("trip_cancel", "denied", "trip", str(trip_id), reason="cancel_charge_owner_only")
        raise Forbidden("cancel_charge_owner_only",
                        "lowering the cancellation charge this much is the agency owner's decision")  # fmt: skip


async def record(ctx: TenantContext, trip_id: uuid.UUID, a: Assessment, invoice_id: uuid.UUID | None) -> uuid.UUID:
    """The run's one ``crm.cancellation_charge`` row (CRM49: written once, by a member)."""
    if a.currency is None:
        raise Conflict("no_accepted_proposal")
    try:
        charge_id = await ctx.conn.fetchval(
            INSERT_CHARGE_SQL, trip_id, a.proposal_id, a.policy["policy_id"] if a.policy is not None else None,
            a.currency, a.total, a.days_before, a.tier.charge_pct if a.tier is not None else None,
            a.policy_charge if a.policy_charge is not None else Decimal("0.00"), a.charge, a.decision, a.reason,
            a.paid, a.kept, a.refund_due, a.invoice_due if invoice_id is not None else Decimal("0.00"), invoice_id,
        )  # fmt: skip
    except asyncpg.UniqueViolationError:
        raise Conflict("cancellation_charged", "this trip's cancellation charge is recorded already") from None
    ctx.audit("cancellation_charge", "allowed", "cancellation_charge", str(charge_id), reason=a.decision)
    return uuid.UUID(str(charge_id))


async def charge_of(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, Any] | None:
    """The recorded charge of a cancelled trip (the trip page; the seam crm-30's trip P&L reads: fee =
    ``charge_amount``)."""
    row = await ctx.conn.fetchrow(CHARGE_SQL, trip_id)
    if row is None:
        return None
    return {
        "charge_id": str(row["charge_id"]),
        "policy_id": str(row["policy_id"]) if row["policy_id"] else None,
        "currency": row["currency"],
        "trip_total": _money(row["trip_total"]),
        "days_before": row["days_before"],
        "charge_pct": None if row["charge_pct"] is None else str(row["charge_pct"]),
        "policy_charge": _money(row["policy_charge"]),
        "charge_amount": _money(row["charge_amount"]),
        "decision": row["decision"],
        "reason": row["reason"],
        "paid_amount": _money(row["paid_amount"]),
        "kept_amount": _money(row["kept_amount"]),
        "refund_due": _money(row["refund_due"]),
        "invoiced_amount": _money(row["invoiced_amount"]),
        "invoice_id": str(row["invoice_id"]) if row["invoice_id"] else None,
        "created_at": _iso(row["created_at"]),
    }
