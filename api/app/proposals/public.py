"""The client's proposal page (C-03 thin + C-04 accept; card CRM-18, AA-946; design v3 M4 base, F6 steps 3 and 5,
§2.9 rules 3 and 5). No account: the lead's signed link (``client_link`` purpose ``proposal``). This module is the only
one that opens :func:`app.tenancy.client_link_tx` for that purpose (``tests/unit/test_code_rules.py``).

- :func:`read`: the agency's name, the proposal text, the currency, total, deposit and balance (deposit and balance
  always apart, rule 5), the booking terms text and version, the ``price_hash`` and the status. A superseded or
  withdrawn version's link was revoked (404 ``link_not_found``). After acceptance: the schedule.
- :func:`accept`: Origin check (the client host), the typed full name, the terms tick, the ``price_hash`` and the terms
  version the page showed (409 ``price_changed`` / ``terms_changed`` when they differ). The proposal moves sent ->
  accepted with the name under the vault DEK; the deposit row (due today in the agency timezone) and the balance row
  (due ``balance_due_on``) are created; the trip owner gets the task ``collect_deposit``; the link is ``submitted``. A
  second accept answers ``already_accepted`` (a second device, a double click).

Responses carry no id but the proposal's own, no net price, no other traveller.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

import asyncpg

from app import stages
from app.errors import Conflict, Invalid, NotFound
from app.intake import stage1
from app.portal.public import check_origin
from app.settings import Settings
from app.tenancy import client_link_tx
from app.vault import KeyProvider, Vault

PURPOSE = "proposal"
MAX_NAME = 200

PROPOSAL_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.status, p.body_enc, p.currency, p.total_amount, p.deposit_amount,
       p.balance_due_on, p.terms_doc_id, p.price_hash, p.accepted_at, t.title, t.start_date, t.end_date,
       t.owner_member_id
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.link_id = $1 AND p.status IN ('sent', 'accepted') AND p.deleted_at IS NULL
   FOR UPDATE OF p
"""
TERMS_SQL = "SELECT doc_id, version_label, body_text, body_sha256 FROM crm.agreement_doc WHERE doc_id = $1"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
OPENED_SQL = """
UPDATE crm.client_link SET status = 'opened', first_opened_at = now() WHERE link_id = $1 AND status = 'sent'
"""
SUBMITTED_SQL = """
UPDATE crm.client_link
   SET status = 'submitted', submitted_at = now(), first_opened_at = coalesce(first_opened_at, now())
 WHERE link_id = $1 AND status IN ('sent', 'opened')
"""
ACCEPT_SQL = """
UPDATE crm.proposal SET status = 'accepted', accepted_at = now(), accepted_name_enc = $2,
       accepted_price_hash = price_hash, accepted_terms_doc_id = terms_doc_id
 WHERE proposal_id = $1
RETURNING accepted_at
"""
TODAY_SQL = """
SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), 'Asia/Ho_Chi_Minh'))::date
"""
SCHEDULE_INSERT_SQL = """
INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, amount, currency, due_on) VALUES ($1, $2, $3, $4, $5, $6)
"""
SCHEDULE_SQL = """
SELECT kind, amount, currency, due_on, status FROM crm.payment_schedule
 WHERE proposal_id = $1 AND status <> 'void' AND deleted_at IS NULL ORDER BY due_on, kind
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
VALUES ($1, 'collect_deposit', 'normal', $2, 'created', now() + interval '2 days')
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


def hash_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _money(value: Any) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def read(pool: asyncpg.Pool, keys: KeyProvider, token: str) -> dict[str, Any]:
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        row = await ctx.conn.fetchrow(PROPOSAL_SQL, link.link_id)
        if row is None:
            raise NotFound("link_not_found")
        await ctx.conn.execute(OPENED_SQL, link.link_id)
        if link.status == "sent":
            ctx.audit("client_link_open", "allowed", "client_link", str(link.link_id))
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        terms = await ctx.conn.fetchrow(TERMS_SQL, row["terms_doc_id"]) if row["terms_doc_id"] else None
        deposit, total = Decimal(row["deposit_amount"]), Decimal(row["total_amount"])
        return {
            "agency_name": await ctx.conn.fetchval(AGENCY_SQL, link.tenant_id),
            "proposal_id": str(row["proposal_id"]),
            "version": row["version"],
            "status": row["status"],
            "trip": {"title": row["title"], "start_date": _iso(row["start_date"]), "end_date": _iso(row["end_date"])},
            "body": await vault.decrypt("proposal", "body_enc", row["proposal_id"], row["body_enc"]),
            "currency": row["currency"],
            "total_amount": _money(total),
            "deposit_amount": _money(deposit),
            "balance_amount": _money(total - deposit),
            "balance_due_on": _iso(row["balance_due_on"]),
            "terms": None
            if terms is None
            else {
                "doc_id": str(terms["doc_id"]),
                "version_label": terms["version_label"],
                "body_text": terms["body_text"],
            },
            "price_hash": bytes(row["price_hash"]).hex(),
            "accepted_at": _iso(row["accepted_at"]),
            "schedule": [
                {
                    "kind": s["kind"],
                    "amount": _money(s["amount"]),
                    "currency": s["currency"],
                    "due_on": s["due_on"].isoformat(),
                    "status": s["status"],
                }
                for s in await ctx.conn.fetch(SCHEDULE_SQL, row["proposal_id"])
            ],
        }


def _checked(raw: Any) -> tuple[str, str, str | None]:
    if not isinstance(raw, dict):
        raise Invalid("invalid_submission", "an object", fields=["body"])
    name = stage1.clean_text(raw.get("full_name"), MAX_NAME) if isinstance(raw.get("full_name"), str) else None
    price = raw.get("price_hash")
    bad = []
    if not name:
        bad.append("full_name")
    if raw.get("accept_terms") is not True:
        bad.append("accept_terms")
    if not (isinstance(price, str) and len(price) == 64):
        bad.append("price_hash")
    terms = raw.get("terms_doc_id")
    if terms is not None and not isinstance(terms, str):
        bad.append("terms_doc_id")
    if bad:
        raise Invalid("invalid_submission", "fields to fix", fields=bad)
    return name, price, terms  # type: ignore[return-value]


async def accept(
    pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, token: str, raw: Any, *, origin: str | None
) -> dict[str, Any]:
    check_origin(settings, origin)
    name, price, terms_doc_id = _checked(raw)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        row = await ctx.conn.fetchrow(PROPOSAL_SQL, link.link_id)
        if row is None:
            raise NotFound("link_not_found")
        if row["status"] == "accepted":
            return {"status": "already_accepted", "accepted_at": _iso(row["accepted_at"])}
        if price != bytes(row["price_hash"]).hex():
            raise Conflict("price_changed", "the proposal changed; read it again")
        if (str(row["terms_doc_id"]) if row["terms_doc_id"] else None) != terms_doc_id:
            raise Conflict("terms_changed", "the terms changed; read them again")
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        sealed = await vault.encrypt("proposal", "accepted_name_enc", row["proposal_id"], name)
        accepted_at = await ctx.conn.fetchval(ACCEPT_SQL, row["proposal_id"], sealed)
        today = await ctx.conn.fetchval(TODAY_SQL)
        deposit, total = Decimal(row["deposit_amount"]), Decimal(row["total_amount"])
        await ctx.conn.execute(
            SCHEDULE_INSERT_SQL, row["trip_id"], row["proposal_id"], "deposit", deposit, row["currency"], today
        )
        if deposit < total:
            await ctx.conn.execute(
                SCHEDULE_INSERT_SQL,
                row["trip_id"],
                row["proposal_id"],
                "balance",
                total - deposit,
                row["currency"],
                max(row["balance_due_on"], today),
            )
        await ctx.conn.execute(TASK_SQL, row["trip_id"], row["owner_member_id"])
        await ctx.conn.execute(SUBMITTED_SQL, link.link_id)
        await stages.touch(ctx, row["trip_id"])
        ctx.audit("proposal_accept", "allowed", "proposal", str(row["proposal_id"]))
        return {"status": "accepted", "accepted_at": _iso(accepted_at)}
