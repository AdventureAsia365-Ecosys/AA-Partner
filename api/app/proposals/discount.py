"""A builder version's discount and the owner's approval above the agency limit (AA-1066, crm-048; root
docs/research/2026-11-10-odoo-tour-travel-plugin-comparison.md item crm-07).

- **The discount** (``crm.proposal.discount_pct``, owner / agent: a money field) comes off the charged total (never
  off the pay-on-arrival items); the deposit, the balance, the client's total and the price hash follow it
  (:func:`app.proposals.builder._charged`).
- **When it needs the owner**: a discount above ``crm.tenant_settings.discount_owner_limit_pct`` (default 10 %), or a
  discounted total below the net (a sale at a loss, known only when every chosen option has a net). The version is
  covered by an approved ``crm.discount_approval`` of the same trip at that percent or more (and, for a sale below
  cost, one that approved that too). A raised discount is no longer covered: the approval re-opens.
- **Who**: an agent asks (:func:`request`: one pending row per version, a newer one supersedes it, an urgent
  ``discount_approval`` task for an active owner); an owner approves or rejects it (:func:`decide`,
  ``Action.DISCOUNT_APPROVE``). An owner who publishes a discount above the limit records their own approval at that
  moment (:func:`owner_record`), so the trip shows who allowed it. Publish by a non-owner without cover is 409
  ``discount_needs_approval``. Every step is in the audit (object kind ``discount_approval``) and the row keeps the
  requester (``created_by``) and the decider (``decided_by``); the database guard (CRM48) lets only an owner decide.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from app.errors import Conflict, NotFound
from app.proposals.pricing import Settings
from app.tenancy import AnyContext, TenantContext

DISCOUNT_TASK = "discount_approval"
APPROVALS_SQL = """
SELECT discount_approval_id, proposal_id, discount_pct, below_cost, limit_pct, status, decided_by, decided_at,
       created_by, created_at
  FROM crm.discount_approval WHERE trip_id = $1 ORDER BY created_at DESC LIMIT 50
"""
APPROVAL_SQL = """
SELECT discount_approval_id, proposal_id, trip_id, discount_pct, below_cost, status
  FROM crm.discount_approval WHERE discount_approval_id = $1 FOR UPDATE
"""
SUPERSEDE_SQL = """
UPDATE crm.discount_approval SET status = 'superseded' WHERE proposal_id = $1 AND status = 'pending'
"""
INSERT_SQL = """
INSERT INTO crm.discount_approval (trip_id, proposal_id, discount_pct, below_cost, limit_pct)
VALUES ($1, $2, $3, $4, $5) RETURNING discount_approval_id
"""
OWNER_SQL = """
INSERT INTO crm.discount_approval (trip_id, proposal_id, discount_pct, below_cost, limit_pct, status, decided_at,
                                   decided_by)
VALUES ($1, $2, $3, $4, $5, 'approved', now(), $6) RETURNING discount_approval_id
"""
DECIDE_SQL = """
UPDATE crm.discount_approval SET status = $2, decided_at = now(), decided_by = $3 WHERE discount_approval_id = $1
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', m.member_id, 'created', now() + interval '1 day'
  FROM crm.member m WHERE m.role = 'owner' AND m.status = 'active'
 ORDER BY m.created_at LIMIT 1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
CLOSE_TASK_SQL = """
UPDATE crm.task SET status = 'done', done_at = now(), done_by = $3
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL
"""


def _pct(value: Any) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01")) if value is not None else Decimal("0.00")


def need(discount_pct: Any, margin_total: Any, limit: Decimal) -> tuple[bool, bool]:
    """``(above the limit, below cost)`` of a version's discount and its discounted margin."""
    pct = _pct(discount_pct)
    below = margin_total is not None and Decimal(margin_total) < 0
    return pct > limit, below


def covered(approvals: list[Mapping[str, Any]], discount_pct: Any, below_cost: bool) -> bool:
    """An approved row of the trip covers this discount (and, for a sale below cost, approved that too)."""
    pct = _pct(discount_pct)
    return any(
        a["status"] == "approved" and Decimal(a["discount_pct"]) >= pct and (a["below_cost"] or not below_cost)
        for a in approvals
    )


async def approvals_of(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    return [dict(r) for r in await ctx.conn.fetch(APPROVALS_SQL, trip_id)]


def _approval_out(a: Mapping[str, Any], *, sees_margin: bool = True) -> dict[str, Any]:
    out = {
        "discount_approval_id": str(a["discount_approval_id"]),
        "proposal_id": str(a["proposal_id"]),
        "discount_pct": str(_pct(a["discount_pct"])),
        "below_cost": bool(a["below_cost"]),
        "limit_pct": str(_pct(a["limit_pct"])),
        "status": a["status"],
        "requested_by": a["created_by"],
        "requested_at": a["created_at"].isoformat() if a.get("created_at") else None,
        "decided_by": a["decided_by"],
        "decided_at": a["decided_at"].isoformat() if a.get("decided_at") else None,
    }
    if not sees_margin:
        out.pop("below_cost")
    return out


async def state(
    ctx: AnyContext,
    *,
    trip_id: uuid.UUID,
    proposal_id: uuid.UUID,
    discount_pct: Any,
    margin_total: Any,
    bands: Settings,
    sees_margin: bool = True,
) -> dict[str, Any]:
    """The builder's discount box: the percent, the limit, whether the owner must approve, the version's pending
    request and the trip's recent decisions. ``below_cost`` is margin data: built only for a role that sees margins
    (the serializer would strip it anyway, like every ``*_cost`` key)."""
    above, below = need(discount_pct, margin_total, bands.discount_owner_limit_pct)
    approvals = await approvals_of(ctx, trip_id) if (above or below) else []
    is_covered = covered(approvals, discount_pct, below)
    pending = next((a for a in approvals if a["status"] == "pending" and a["proposal_id"] == proposal_id), None)
    approved = [Decimal(a["discount_pct"]) for a in approvals if a["status"] == "approved"]
    out = {
        "discount_pct": str(_pct(discount_pct)) if discount_pct is not None else None,
        "limit_pct": str(_pct(bands.discount_owner_limit_pct)),
        "above_limit": above,
        "below_cost": below,
        "needs_approval": (above or below) and not is_covered,
        "approved_pct": str(max(approved)) if approved else None,
        "pending": _approval_out(pending, sees_margin=sees_margin) if pending else None,
        "history": [_approval_out(a, sees_margin=sees_margin) for a in approvals[:5]],
    }
    if not sees_margin:
        out.pop("below_cost")
    return out


async def request(
    ctx: TenantContext, *, trip_id: uuid.UUID, proposal_id: uuid.UUID, discount_pct: Any, margin_total: Any,
    bands: Settings,
) -> uuid.UUID:  # fmt: skip
    """An agent asks the owner (the draft is locked by the caller). Not needed = 409 ``discount_approval_not_needed``;
    the same request again returns the pending one; a different one supersedes it."""
    above, below = need(discount_pct, margin_total, bands.discount_owner_limit_pct)
    approvals = await approvals_of(ctx, trip_id)
    if not (above or below) or covered(approvals, discount_pct, below):
        raise Conflict("discount_approval_not_needed", "this discount is within the limit or already approved")
    pct = _pct(discount_pct)
    for a in approvals:
        if (a["status"] == "pending" and a["proposal_id"] == proposal_id and _pct(a["discount_pct"]) == pct
                and bool(a["below_cost"]) == below):  # fmt: skip
            return uuid.UUID(str(a["discount_approval_id"]))
    await ctx.conn.execute(SUPERSEDE_SQL, proposal_id)
    approval_id = await ctx.conn.fetchval(INSERT_SQL, trip_id, proposal_id, pct, below,
                                          bands.discount_owner_limit_pct)  # fmt: skip
    await ctx.conn.execute(TASK_SQL, trip_id, DISCOUNT_TASK)
    ctx.audit("discount_request", "allowed", "discount_approval", str(approval_id), reason="above_limit" if above
              else "below_cost")  # fmt: skip
    return uuid.UUID(str(approval_id))


async def decide(
    ctx: TenantContext, approval_id: uuid.UUID, decision: str, *, proposal_id: uuid.UUID
) -> dict[str, Any]:
    """The owner's decision on a pending request of this version (the role is checked by the route and the
    database)."""
    row = await ctx.conn.fetchrow(APPROVAL_SQL, approval_id)
    if row is None or row["proposal_id"] != proposal_id:
        raise NotFound("discount_approval_not_found")
    if row["status"] != "pending":
        raise Conflict("discount_approval_not_pending", "this request was decided or replaced already")
    actor = f"member:{ctx.member_id}"
    await ctx.conn.execute(DECIDE_SQL, approval_id, decision, actor)
    await ctx.conn.execute(CLOSE_TASK_SQL, row["trip_id"], DISCOUNT_TASK, actor)
    ctx.audit("discount_decide", "allowed", "discount_approval", str(approval_id), reason=decision)
    return {"discount_approval_id": str(approval_id), "status": decision}


async def owner_record(
    ctx: TenantContext, *, trip_id: uuid.UUID, proposal_id: uuid.UUID, discount_pct: Any, below_cost: bool,
    bands: Settings,
) -> None:  # fmt: skip
    """An owner publishes a discount the limit does not cover: their own approval, recorded at that moment."""
    actor = f"member:{ctx.member_id}"
    await ctx.conn.execute(SUPERSEDE_SQL, proposal_id)
    approval_id = await ctx.conn.fetchval(OWNER_SQL, trip_id, proposal_id, _pct(discount_pct), below_cost,
                                          bands.discount_owner_limit_pct, actor)  # fmt: skip
    await ctx.conn.execute(CLOSE_TASK_SQL, trip_id, DISCOUNT_TASK, actor)
    ctx.audit("discount_decide", "allowed", "discount_approval", str(approval_id), reason="owner_set")
