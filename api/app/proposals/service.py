"""Text proposals, the payment schedule and manual payments, the agency side (card CRM-18, AA-946; design v3 M4 base,
M7 manual, F6; A-05 / A-10 thin). Routes in :mod:`app.routers.proposals`; the client's page in
:mod:`app.proposals.public`.

- **Draft** (``PROPOSAL_DRAFT``, every role): a new version per trip (text under the vault DEK, currency, sell total,
  deposit, balance date, optional booking terms version). Only a draft is edited. A trip that already has an accepted
  proposal takes no new version (409 ``proposal_already_accepted``).
- **Send** (``PROPOSAL_PUBLISH``, owner / agent): the trip must be at Proposal (409 ``trip_not_in_proposal``); the
  terms version must be published; ``price_hash`` is fixed; an earlier sent version is superseded (its link revoked);
  a signed link (purpose ``proposal``, 14 days) for the lead goes through the dispatcher and its path is returned once
  ("Copy link"). **Withdraw** a draft or a sent version.
- **Mark paid** (``PAYMENT_MARK_RECEIVED``, owner / agent): one ``crm.payment`` row, the schedule row paid, a
  ``trip_commitment`` (kind ``payment``, succeeded: the delete block). A paid deposit of the accepted proposal moves a
  trip at Proposal to Booked (reason ``deposit_paid``) and invites every traveller to the portal (F6.6).
- **Void** a due balance row (owner / agent). A deposit row is never void (409 ``deposit_not_voidable``; crm-016
  ``payment_schedule_deposit_void_chk``): a deposit taken outside the CRM is marked paid with its method (cash /
  other), so the accepted proposal always keeps its way to Booked and to the portal (review S251).

Every function takes the open context and never opens one. Amounts are the client's sell amounts only (no net, cost
or margin key: nothing for the margin serializer to strip).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from app import stages
from app.dispatch import queue as dispatch
from app.errors import Conflict, Invalid, NotFound
from app.portal import access, progress
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

LINK_PURPOSE = "proposal"
LINK_DAYS = 14
MAX_BODY = 20_000
MAX_VERSIONS = 999
DRAFTABLE_STAGES = ("inquiry", "qualifying", "proposal")
METHODS = ("bank_transfer", "cash", "card_offline", "other")
SEALED_SQLSTATE = "CRM31"

TRIP_SQL = """
SELECT trip_id, stage, owner_member_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
LIST_SQL = """
SELECT proposal_id, version, status, body_enc, currency, total_amount, deposit_amount, balance_due_on, terms_doc_id,
       price_hash, sent_at, accepted_at, created_at, updated_at
  FROM crm.proposal WHERE trip_id = $1 AND deleted_at IS NULL
 ORDER BY version DESC
 LIMIT 50
"""
ONE_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.status, p.body_enc, p.currency, p.total_amount, p.deposit_amount,
       p.balance_due_on, p.terms_doc_id, p.price_hash, p.link_id, t.stage, t.owner_member_id
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.proposal_id = $1 AND p.deleted_at IS NULL
   FOR UPDATE OF p
"""
ACCEPTED_EXISTS_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.proposal WHERE trip_id = $1 AND status = 'accepted' AND deleted_at IS NULL)
"""
NEXT_VERSION_SQL = "SELECT coalesce(max(version), 0) + 1 FROM crm.proposal WHERE trip_id = $1"
INSERT_SQL = """
INSERT INTO crm.proposal (proposal_id, trip_id, version, body_enc, currency, total_amount, deposit_amount,
                          balance_due_on, terms_doc_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
UPDATE_SQL = """
UPDATE crm.proposal SET body_enc = $2, currency = $3, total_amount = $4, deposit_amount = $5, balance_due_on = $6,
       terms_doc_id = $7
 WHERE proposal_id = $1
"""
TERMS_SQL = """
SELECT doc_id, status, version_label, body_sha256 FROM crm.agreement_doc
 WHERE doc_id = $1 AND kind = 'booking_terms' AND deleted_at IS NULL
"""
OPEN_SENT_SQL = """
SELECT proposal_id, link_id FROM crm.proposal WHERE trip_id = $1 AND status = 'sent' AND deleted_at IS NULL FOR UPDATE
"""
SUPERSEDE_SQL = "UPDATE crm.proposal SET status = 'superseded' WHERE proposal_id = $1"
REVOKE_LINK_SQL = """
UPDATE crm.client_link SET status = 'revoked', revoked_at = now()
 WHERE link_id = $1 AND status IN ('sent', 'opened', 'submitted')
"""
INSERT_LINK_SQL = """
INSERT INTO crm.client_link (link_id, trip_id, traveler_id, purpose, token_hash, expires_at, reissued_from)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(days => $6), $7)
RETURNING expires_at
"""
LINK_DISPATCH_SQL = "UPDATE crm.client_link SET dispatch_id = $2 WHERE link_id = $1"
SEND_SQL = """
UPDATE crm.proposal SET status = 'sent', price_hash = $2, sent_at = now(), link_id = $3 WHERE proposal_id = $1
RETURNING sent_at
"""
WITHDRAW_SQL = "UPDATE crm.proposal SET status = 'withdrawn' WHERE proposal_id = $1"
LEAD_SQL = "SELECT traveler_id FROM crm.trip_traveler WHERE trip_id = $1 AND role = 'lead' AND deleted_at IS NULL"
SCHEDULE_SQL = """
SELECT schedule_id, proposal_id, kind, amount, currency, due_on, status, paid_at FROM crm.payment_schedule
 WHERE trip_id = $1 AND deleted_at IS NULL
 ORDER BY due_on, kind
"""
PAYMENTS_SQL = """
SELECT payment_id, schedule_id, amount, currency, method, received_on, created_at FROM crm.payment
 WHERE trip_id = $1 ORDER BY received_on, created_at
"""
SCHEDULE_ONE_SQL = """
SELECT s.schedule_id, s.trip_id, s.proposal_id, s.kind, s.amount, s.currency, s.status, p.status AS proposal_status,
       t.stage
  FROM crm.payment_schedule s
  JOIN crm.proposal p ON p.proposal_id = s.proposal_id
  JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
 WHERE s.schedule_id = $1 AND s.deleted_at IS NULL
   FOR UPDATE OF s
"""
PAY_SQL = "UPDATE crm.payment_schedule SET status = 'paid', paid_at = now() WHERE schedule_id = $1"
VOID_SQL = "UPDATE crm.payment_schedule SET status = 'void' WHERE schedule_id = $1"
INSERT_PAYMENT_SQL = """
INSERT INTO crm.payment (payment_id, trip_id, schedule_id, amount, currency, method, received_on)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
COMMITMENT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, 'payment', 'succeeded', $2)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""
CLOSE_TASK_SQL = """
UPDATE crm.task SET status = 'done', done_at = now(), done_by = $3
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL
"""
TODAY_SQL = """
SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), 'Asia/Ho_Chi_Minh'))::date
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Any) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def price_hash(
    *,
    proposal_id: uuid.UUID,
    version: int,
    currency: str,
    total: Any,
    deposit: Any,
    balance_due_on: dt.date | None,
    body: str,
    terms_doc_id: uuid.UUID | None,
    terms_sha256: bytes | None,
) -> bytes:
    """SHA-256 of the canonical record the client sees (design M3 ``price_hash``; acceptance sends it back)."""
    record = {
        "proposal_id": str(proposal_id),
        "version": version,
        "currency": currency,
        "total": _money(total),
        "deposit": _money(deposit),
        "balance_due_on": _iso(balance_due_on),
        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "terms_doc_id": str(terms_doc_id) if terms_doc_id else None,
        "terms_sha256": terms_sha256.hex() if terms_sha256 else None,
    }
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).digest()


def _check_amounts(currency: str, total: Decimal, deposit: Decimal, balance_due_on: dt.date | None) -> None:
    if not (len(currency) == 3 and currency.isalpha() and currency.isupper()):
        raise Invalid("invalid_currency", "a three-letter code (ISO 4217)")
    if total <= 0 or deposit <= 0:
        raise Invalid("invalid_amount", "total and deposit are above zero")
    if deposit > total:
        raise Invalid("deposit_above_total", "the deposit is at most the total")
    if deposit < total and balance_due_on is None:
        raise Invalid("balance_due_on_required", "a balance is left: when is it due?")
    if deposit == total and balance_due_on is not None:
        raise Invalid("balance_due_on_not_allowed", "nothing is left after the deposit")


def _body(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise Invalid("body_required", "write the proposal text")
    clean = text.replace("\r\n", "\n").strip()
    if len(clean) > MAX_BODY:
        raise Invalid("body_too_long", f"at most {MAX_BODY} characters")
    return clean


async def _terms(ctx: AnyContext, terms_doc_id: uuid.UUID | None, *, published: bool) -> Any:
    if terms_doc_id is None:
        return None
    row = await ctx.conn.fetchrow(TERMS_SQL, terms_doc_id)
    if row is None:
        raise NotFound("terms_not_found")
    if published and row["status"] != "published":
        raise Conflict("terms_not_published", "publish the booking terms first (Settings > Agreements)")
    return row


async def _view(vault: Vault, row: Any) -> dict[str, Any]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "version": row["version"],
        "status": row["status"],
        "body": await vault.decrypt("proposal", "body_enc", row["proposal_id"], row["body_enc"]),
        "currency": row["currency"],
        "total_amount": _money(row["total_amount"]),
        "deposit_amount": _money(row["deposit_amount"]),
        "balance_due_on": _iso(row["balance_due_on"]),
        "terms_doc_id": str(row["terms_doc_id"]) if row["terms_doc_id"] else None,
        "price_hash": bytes(row["price_hash"]).hex() if row["price_hash"] else None,
        "sent_at": _iso(row["sent_at"]),
        "accepted_at": _iso(row["accepted_at"]),
    }


def _schedule_view(r: Any) -> dict[str, Any]:
    return {
        "schedule_id": str(r["schedule_id"]),
        "proposal_id": str(r["proposal_id"]),
        "kind": r["kind"],
        "amount": _money(r["amount"]),
        "currency": r["currency"],
        "due_on": _iso(r["due_on"]),
        "status": r["status"],
        "paid_at": _iso(r["paid_at"]),
    }


async def trip_overview(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID) -> dict[str, Any]:
    """The trip's proposals (newest first), payment schedule, payments and portal progress: one ``view`` event."""
    trip = await ctx.conn.fetchrow(
        "SELECT trip_id, stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL", trip_id
    )
    if trip is None:
        raise NotFound("trip_not_found")
    proposals = [await _view(vault, r) for r in await ctx.conn.fetch(LIST_SQL, trip_id)]
    payments = [
        {
            "payment_id": str(r["payment_id"]),
            "schedule_id": str(r["schedule_id"]),
            "amount": _money(r["amount"]),
            "currency": r["currency"],
            "method": r["method"],
            "received_on": _iso(r["received_on"]),
        }
        for r in await ctx.conn.fetch(PAYMENTS_SQL, trip_id)
    ]
    portal = await progress.refresh(ctx, trip_id)
    ctx.audit("view", "allowed", "trip", str(trip_id), fields=("proposals", "payments", "portal"))
    return {
        "trip_id": str(trip_id),
        "stage": trip["stage"],
        "proposals": proposals,
        "schedule": [_schedule_view(r) for r in await ctx.conn.fetch(SCHEDULE_SQL, trip_id)],
        "payments": payments,
        "deposit_paid": bool(await ctx.conn.fetchval(stages.DEPOSIT_PAID_SQL, trip_id)),
        "portal_unlocked": bool(await ctx.conn.fetchval(stages.PORTAL_UNLOCKED_SQL, trip_id)),
        "portal": portal.view(),
    }


async def create_draft(
    ctx: TenantContext,
    vault: Vault,
    trip_id: uuid.UUID,
    *,
    body: Any,
    currency: str,
    total: Decimal,
    deposit: Decimal,
    balance_due_on: dt.date | None,
    terms_doc_id: uuid.UUID | None,
) -> dict[str, Any]:
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    if trip["stage"] not in DRAFTABLE_STAGES:
        raise Conflict("trip_not_open_for_proposal", "a proposal is drafted before the trip is booked")
    if await ctx.conn.fetchval(ACCEPTED_EXISTS_SQL, trip_id):
        raise Conflict("proposal_already_accepted", "the client accepted a version already")
    text = _body(body)
    _check_amounts(currency, total, deposit, balance_due_on)
    await _terms(ctx, terms_doc_id, published=False)
    version = int(await ctx.conn.fetchval(NEXT_VERSION_SQL, trip_id))
    if version > MAX_VERSIONS:
        raise Conflict("too_many_versions")
    proposal_id = uuid.uuid4()
    await ctx.conn.execute(
        INSERT_SQL,
        proposal_id,
        trip_id,
        version,
        await vault.encrypt("proposal", "body_enc", proposal_id, text),
        currency,
        total,
        deposit,
        balance_due_on,
        terms_doc_id,
    )
    await stages.touch(ctx, trip_id)
    ctx.audit("proposal_draft", "allowed", "proposal", str(proposal_id))
    return {"proposal_id": str(proposal_id), "version": version, "status": "draft"}


async def update_draft(
    ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["status"] != "draft":
        raise Conflict("proposal_not_draft", "only a draft is edited; draft a new version")
    text = (
        _body(changes["body"])
        if "body" in changes
        else await vault.decrypt("proposal", "body_enc", proposal_id, row["body_enc"])
    )
    currency = changes.get("currency", row["currency"])
    total = Decimal(changes.get("total_amount", row["total_amount"]))
    deposit = Decimal(changes.get("deposit_amount", row["deposit_amount"]))
    balance = changes["balance_due_on"] if "balance_due_on" in changes else row["balance_due_on"]
    terms = changes["terms_doc_id"] if "terms_doc_id" in changes else row["terms_doc_id"]
    _check_amounts(currency, total, deposit, balance)
    await _terms(ctx, terms, published=False)
    await ctx.conn.execute(
        UPDATE_SQL,
        proposal_id,
        await vault.encrypt("proposal", "body_enc", proposal_id, text),
        currency,
        total,
        deposit,
        balance,
        terms,
    )
    ctx.audit("proposal_draft", "allowed", "proposal", str(proposal_id), reason="edit")
    return {"proposal_id": str(proposal_id), "status": "draft"}


async def send(ctx: TenantContext, vault: Vault, proposal_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["status"] != "draft":
        raise Conflict("proposal_not_draft", "only a draft is sent; draft a new version")
    if row["stage"] != "proposal":
        raise Conflict("trip_not_in_proposal", "move the trip to Proposal first (the Stage 2 gate)")
    trip_id = row["trip_id"]
    lead = await ctx.conn.fetchval(LEAD_SQL, trip_id)
    if lead is None:
        raise Conflict("no_lead_traveller", "the trip has no lead traveller to send the proposal to")
    terms = await _terms(ctx, row["terms_doc_id"], published=True)
    body = await vault.decrypt("proposal", "body_enc", proposal_id, row["body_enc"])
    digest = price_hash(
        proposal_id=proposal_id,
        version=row["version"],
        currency=row["currency"],
        total=row["total_amount"],
        deposit=row["deposit_amount"],
        balance_due_on=row["balance_due_on"],
        body=body,
        terms_doc_id=row["terms_doc_id"],
        terms_sha256=bytes(terms["body_sha256"]) if terms is not None else None,
    )
    previous = await ctx.conn.fetchrow(OPEN_SENT_SQL, trip_id)
    if previous is not None:
        await ctx.conn.execute(SUPERSEDE_SQL, previous["proposal_id"])
        if previous["link_id"] is not None:
            await ctx.conn.execute(REVOKE_LINK_SQL, previous["link_id"])
    token = secrets.token_urlsafe(32)
    link_id = uuid.uuid4()
    expires_at = await ctx.conn.fetchval(
        INSERT_LINK_SQL,
        link_id,
        trip_id,
        lead,
        LINK_PURPOSE,
        access.token_hash(token),
        LINK_DAYS,
        previous["link_id"] if previous is not None else None,
    )
    queued = await dispatch.enqueue_message(
        ctx,
        traveler_id=lead,
        channel="email",
        purpose="transactional",
        origin="user_action",
        trip_id=trip_id,
        message_ref=link_id,
    )
    await ctx.conn.execute(LINK_DISPATCH_SQL, link_id, uuid.UUID(queued["dispatch_id"]))
    sent_at = await ctx.conn.fetchval(SEND_SQL, proposal_id, digest, link_id)
    await stages.refresh_waiting_on(ctx, trip_id)
    await stages.touch(ctx, trip_id)
    ctx.audit("proposal_send", "allowed", "proposal", str(proposal_id))
    return {
        "proposal_id": str(proposal_id),
        "status": "sent",
        "price_hash": digest.hex(),
        "sent_at": sent_at.isoformat(),
        "link_path": f"/c/p/{token}",
        "expires_at": expires_at.isoformat(),
        "dispatch_id": queued["dispatch_id"],
        "superseded": str(previous["proposal_id"]) if previous is not None else None,
    }


async def withdraw(ctx: TenantContext, proposal_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, proposal_id)
    if row is None:
        raise NotFound("proposal_not_found")
    if row["status"] not in ("draft", "sent"):
        raise Conflict("proposal_not_open", "an accepted, superseded or withdrawn version stays as it is")
    await ctx.conn.execute(WITHDRAW_SQL, proposal_id)
    if row["link_id"] is not None:
        await ctx.conn.execute(REVOKE_LINK_SQL, row["link_id"])
    ctx.audit("proposal_withdraw", "allowed", "proposal", str(proposal_id))
    return {"proposal_id": str(proposal_id), "status": "withdrawn"}


async def mark_paid(
    ctx: TenantContext,
    vault: Vault,
    schedule_id: uuid.UUID,
    *,
    received_on: dt.date | None,
    method: str,
) -> dict[str, Any]:
    if method not in METHODS:
        raise Invalid("invalid_method", ", ".join(METHODS))
    row = await ctx.conn.fetchrow(SCHEDULE_ONE_SQL, schedule_id)
    if row is None:
        raise NotFound("schedule_not_found")
    if row["status"] != "due":
        raise Conflict("schedule_not_due", "this row is paid or void already")
    if row["proposal_status"] != "accepted":
        raise Conflict("proposal_not_accepted")
    today = await ctx.conn.fetchval(TODAY_SQL)
    day = received_on or today
    if day > today:
        raise Invalid("received_in_future", "the day the money arrived, today or earlier")
    payment_id = uuid.uuid4()
    await ctx.conn.execute(PAY_SQL, schedule_id)
    await ctx.conn.execute(
        INSERT_PAYMENT_SQL, payment_id, row["trip_id"], schedule_id, row["amount"], row["currency"], method, day
    )
    await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], f"payment:{payment_id}")
    actor = f"member:{ctx.member_id}"
    ctx.audit("payment_mark_received", "allowed", "payment_schedule", str(schedule_id), reason=row["kind"])
    out: dict[str, Any] = {
        "schedule_id": str(schedule_id),
        "payment_id": str(payment_id),
        "status": "paid",
        "trip_moved": None,
        "portal": None,
    }
    if row["kind"] == "deposit":
        await ctx.conn.execute(CLOSE_TASK_SQL, row["trip_id"], "collect_deposit", actor)
        if row["stage"] == "proposal":
            moved = await stages.move(ctx, row["trip_id"], "booked", vault=vault, reason="deposit_paid")
            out["trip_moved"] = {"from_stage": moved["from_stage"], "stage": moved["stage"],
                                 "waiting_on": moved["waiting_on"]}  # fmt: skip
        out["portal"] = await access.invite_all(ctx, row["trip_id"])
        await stages.refresh_waiting_on(ctx, row["trip_id"])
    await stages.touch(ctx, row["trip_id"])
    return out


async def void(ctx: TenantContext, schedule_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(SCHEDULE_ONE_SQL, schedule_id)
    if row is None:
        raise NotFound("schedule_not_found")
    if row["status"] != "due":
        raise Conflict("schedule_not_due", "a paid row stays (refunds: CRM-23)")
    if row["kind"] == "deposit":
        raise Conflict(
            "deposit_not_voidable",
            "a deposit stays: mark it paid with the method used (cash, other) when it was taken outside the CRM, "
            "or mark the trip lost (or cancel it once booked)",
        )
    try:
        await ctx.conn.execute(VOID_SQL, schedule_id)
    except asyncpg.PostgresError as exc:  # pragma: no cover — the status check above runs first
        if getattr(exc, "sqlstate", None) == SEALED_SQLSTATE:
            raise Conflict("schedule_not_due") from None
        raise
    ctx.audit("payment_schedule_void", "allowed", "payment_schedule", str(schedule_id))
    return {"schedule_id": str(schedule_id), "status": "void"}
