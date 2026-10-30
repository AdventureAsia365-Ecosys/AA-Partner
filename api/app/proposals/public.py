"""The client's proposal page (C-03 / C-04 / C-12; cards CRM-18 AA-946 and CRM-21b AA-950; design v3 M3 client side,
F5, F6, §2.9). No account: the lead's signed link (``client_link`` purpose ``proposal``). This module is the only one
that opens :func:`app.tenancy.client_link_tx` for that purpose (``tests/unit/test_code_rules.py``).

The web exchanges the link's token for a host-only cookie and strips it from the URL (Fix B11); the token stays valid
until the link expires, so a second device (or the spouse) opening the same link gets in too — nothing binds a link
to a device. The agent's "Re-issue link" revokes the old token (:func:`app.proposals.service.reissue_link`).

- :func:`read`: a text version (AA-946: text, total, deposit, balance, terms) or a builder version (AA-950,
  :mod:`app.proposals.client_page`: days, Keep / Swap with the deltas of D1, groups of R26, the hold box, the notice of
  a new version with what changed; AA-951: ``before_you_go``, the day 0 packs and reminders of
  :func:`app.proposals.prep_packs.client_view`, the same as the e-mail / WhatsApp text). Logs "viewed" (R4): the
  counters on the version, one ``proposal.viewed`` event an hour, the first one rings the trip owner's bell.
- :func:`pick`: Keep / Swap / Choose later on a builder version (a sold-out option 409, a fixed block 422, choose
  later on an AA item 422 (Fix B9), a rule break 422 ``option_conflict``); a bell item for the agent (bundled per
  version at read time).
- :func:`hold`: the version's one hold set (:mod:`app.proposals.holds`): ``(status, body)``: 200 ``held`` /
  ``not_needed``; 409 ``option_sold_out`` with the fallback (card E); 429 ``recheck_limit`` ("Ask your agent", card
  R); 409 ``option_conflict`` (the choice breaks a rule: nothing is held); 409 ``hold_in_progress`` (another screen
  is placing this version's hold); 409 ``hold_unavailable`` / 503 ``booking_unavailable`` (the agent is told). A
  request whose answer was lost is replayed with its key and quote, so a hold Booking made is recovered.
- :func:`accept`: Origin check, typed full name, terms tick, the ``price_hash`` and terms version the page showed
  (409 ``price_changed`` / ``terms_changed``). A builder version also needs the ``selection_hash`` the page showed (409
  ``choice_changed``), a choice that keeps the agency's rules (409 ``option_conflict``: card E's fallback may break
  one), the rooms confirmed (Fix B10), no hold request of the version running (409 ``hold_in_progress``: another
  screen may be releasing the very hold this page shows), and, when AA items are chosen, a live hold covering them:
  409 ``hold_required`` / ``hold_expired`` with the re-quote offer (card R). The schedule follows the accepted picks.
- :func:`change_request`: "Ask for a change" (F5.4, R19): one bundled diff of the client's choice for the agent (an
  event with the bell and a task); a reason code, never free text.

Responses carry no id but the proposal's own and the option / block ids of its page, no net price, no other traveller.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from app import stages
from app.booking import proposal_lines
from app.booking.client import BookingClient
from app.errors import Conflict, Invalid, NotFound
from app.intake import stage1
from app.portal.public import check_origin
from app.proposals import builder, client_page, events, holds, prep_packs
from app.settings import Settings
from app.tenancy import AnyContext, client_link_tx
from app.vault import KeyProvider, Vault

PURPOSE = "proposal"
MAX_NAME = 200
VIEW_EVENT_EVERY_S = 3600
OUTAGE_EVENT_EVERY_S = 3600
CHANGE_REASONS = ("rooms", "dates", "activities", "lodging", "budget", "other")

PROPOSAL_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.status, p.kind, p.body_enc, p.currency, p.total_amount,
       p.deposit_amount, p.balance_due_on, p.terms_doc_id, p.price_hash, p.accepted_at, p.price_visibility,
       p.room_config, p.deposit_pct, p.starts_on, p.view_count, p.first_viewed_at, p.hold_id, p.hold_quote_id,
       p.hold_expires_at, p.hold_selection_sha256, p.hold_count, p.recheck_day, p.recheck_count_day,
       p.hold_attempt, p.hold_pending_key, p.hold_pending_at, p.hold_pending_quote_id, p.hold_pending_price_hash,
       p.hold_pending_selection_sha256, p.hold_release_id,
       p.accepted_total_amount, p.accepted_deposit_amount, p.link_id,
       t.title, t.start_date, t.end_date, t.owner_member_id, t.party_adults, t.party_child_ages
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.link_id = $1 AND p.status IN ('sent', 'accepted') AND p.deleted_at IS NULL
   FOR UPDATE OF p
"""
PREVIOUS_SQL = """
SELECT proposal_id, version, currency, deposit_pct, balance_due_on FROM crm.proposal
 WHERE link_id = $1 AND trip_id = $2 AND version < $3 AND status = 'superseded' AND kind = 'builder'
   AND deleted_at IS NULL
 ORDER BY version DESC LIMIT 1
"""
CARRIED_SQL = "SELECT count(*) FROM crm.selection WHERE proposal_id = $1 AND chosen_by = 'carried'"
TERMS_SQL = "SELECT doc_id, version_label, body_text, body_sha256 FROM crm.agreement_doc WHERE doc_id = $1"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
OPENED_SQL = """
UPDATE crm.client_link SET status = 'opened', first_opened_at = now() WHERE link_id = $1 AND status = 'sent'
"""
VIEWED_SQL = """
UPDATE crm.proposal SET view_count = view_count + 1, first_viewed_at = coalesce(first_viewed_at, now()),
       last_viewed_at = now()
 WHERE proposal_id = $1
RETURNING view_count
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
ACCEPT_BUILDER_SQL = """
UPDATE crm.proposal SET status = 'accepted', accepted_at = now(), accepted_name_enc = $2,
       accepted_price_hash = price_hash, accepted_terms_doc_id = terms_doc_id, accepted_selection_sha256 = $3,
       accepted_total_amount = $4, accepted_deposit_amount = $5, accepted_hold_id = $6
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
VALUES ($1, 'collect_deposit', 'normal', $2, $3, coalesce($4, now() + interval '2 days'))
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
CHANGE_TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
VALUES ($1, 'proposal_change_request', 'normal', $2, 'created', now() + interval '1 day')
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
HOLD_TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
VALUES ($1, 'proposal_hold_problem', 'urgent', $2, 'created', now() + interval '4 hours')
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


def hash_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _money(value: Any) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _row(ctx: AnyContext, link: Any) -> Any:
    row = await ctx.conn.fetchrow(PROPOSAL_SQL, link.link_id)
    if row is None:
        raise NotFound("link_not_found")
    return row


async def _builder_state(ctx: AnyContext, row: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    s = await builder._structure(ctx, row["proposal_id"])
    picks = [dict(r) for r in await ctx.conn.fetch(builder.PICKS_SQL, row["proposal_id"])]
    return s, picks


async def _notice(ctx: AnyContext, row: Any, s: dict[str, Any], total_now: str) -> dict[str, Any] | None:
    """A new version on the same link: what changed since the version the client saw (one bundled notice). The totals
    are the client's: the superseded version with the picks they had made on it, and this page's total (their picks
    carried over included), never the published (defaults') totals."""
    prev = await ctx.conn.fetchrow(PREVIOUS_SQL, row["link_id"], row["trip_id"], row["version"])
    if prev is None:
        return None
    before = await builder._structure(ctx, prev["proposal_id"])
    before_picks = [dict(r) for r in await ctx.conn.fetch(builder.PICKS_SQL, prev["proposal_id"])]
    total_before = client_page.totals(before, prev, client_page.effective(before, before_picks)["picks"])["total"]
    carried = int(await ctx.conn.fetchval(CARRIED_SQL, row["proposal_id"]))
    notice = client_page.version_notice(before, s, total_before, total_now, carried)
    notice["from_version"] = prev["version"]
    return notice


async def _page(ctx: AnyContext, keys: KeyProvider, link: Any, row: Any) -> dict[str, Any]:
    vault = Vault(keys, ctx)  # type: ignore[arg-type]
    terms = await ctx.conn.fetchrow(TERMS_SQL, row["terms_doc_id"]) if row["terms_doc_id"] else None
    out: dict[str, Any] = {
        "agency_name": await ctx.conn.fetchval(AGENCY_SQL, link.tenant_id),
        "proposal_id": str(row["proposal_id"]),
        "version": row["version"],
        "status": row["status"],
        "kind": row["kind"],
        "trip": {"title": row["title"], "start_date": _iso(row["start_date"]), "end_date": _iso(row["end_date"])},
        "body": await vault.decrypt("proposal", "body_enc", row["proposal_id"], row["body_enc"]),
        "currency": row["currency"],
        "terms": None
        if terms is None
        else {"doc_id": str(terms["doc_id"]), "version_label": terms["version_label"], "body_text": terms["body_text"]},
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
    if row["kind"] == "builder":
        s, picks = await _builder_state(ctx, row)
        out.update(await client_page.view(ctx, vault, row, s, picks, _now()))
        # R38 / F4: the same "Before you go" as the e-mail and WhatsApp texts (packs while the trip has AA items, a
        # visa section = its official links only, the insurance reminder, the fitness warning).
        out["before_you_go"] = await prep_packs.client_view(ctx, row["proposal_id"], row["trip_id"])
        t = out["totals"]
        out["notice"] = await _notice(ctx, row, s, t["total"]) if row["status"] == "sent" else None
        out["total_amount"], out["deposit_amount"] = t["total"], t["deposit"]
        out["balance_amount"], out["balance_due_on"] = t["balance"], t["balance_due_on"]
        if row["status"] == "accepted":
            out["total_amount"] = _money(row["accepted_total_amount"])
            out["deposit_amount"] = _money(row["accepted_deposit_amount"])
            out["balance_amount"] = _money(row["accepted_total_amount"] - row["accepted_deposit_amount"])
    else:
        deposit, total = Decimal(row["deposit_amount"]), Decimal(row["total_amount"])
        out.update(
            total_amount=_money(total),
            deposit_amount=_money(deposit),
            balance_amount=_money(total - deposit),
            balance_due_on=_iso(row["balance_due_on"]),
        )
    return out


async def read(pool: asyncpg.Pool, keys: KeyProvider, token: str) -> dict[str, Any]:
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        row = await _row(ctx, link)
        await ctx.conn.execute(OPENED_SQL, link.link_id)
        if link.status == "sent":
            ctx.audit("client_link_open", "allowed", "client_link", str(link.link_id))
        if row["status"] == "sent":
            # R4: viewed. The counters move on every read; the event at most once an hour; the first one rings.
            first = row["view_count"] == 0
            await ctx.conn.execute(VIEWED_SQL, row["proposal_id"])
            if first or not await events.recent(ctx, row["proposal_id"], events.VIEWED, VIEW_EVENT_EVERY_S):
                await events.record(ctx, row["trip_id"], events.VIEWED, proposal_id=row["proposal_id"],
                                    detail={"version": row["version"], "first": first}, bell=first)  # fmt: skip
        return await _page(ctx, keys, link, row)


def _require_builder_open(row: Any) -> None:
    if row["kind"] != "builder":
        raise Conflict("proposal_not_interactive", "this proposal has no options to choose")
    if row["status"] != "sent":
        raise Conflict("already_accepted", "this proposal was accepted already")


async def pick(
    pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, token: str, raw: Any, *, origin: str | None
) -> dict[str, Any]:
    """Keep / Swap / Choose later. Body ``{"block_id", "option_id"}`` or ``{"block_id", "choose_later": true}``."""
    check_origin(settings, origin)
    if not isinstance(raw, dict) or not isinstance(raw.get("block_id"), str):
        raise Invalid("invalid_submission", "block_id", fields=["block_id"])
    try:
        block_id = uuid.UUID(raw["block_id"])
        option_raw = raw.get("option_id")
        option_id = uuid.UUID(option_raw) if isinstance(option_raw, str) else None
    except ValueError:
        raise Invalid("invalid_submission", "ids", fields=["block_id", "option_id"]) from None
    later = raw.get("choose_later") is True
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        row = await _row(ctx, link)
        _require_builder_open(row)
        s = await builder._structure(ctx, row["proposal_id"])
        block = next((b for b in s["blocks"] if b["option_block_id"] == block_id), None)
        if block is None:
            raise NotFound("option_not_found")
        if later:
            # Fix B9: only an agency item may wait; it is the block's default, marked for later.
            if not client_page.choose_later_ok(block, builder._option_map(s)):
                raise Invalid("choose_later_not_allowed", "this item is decided when you accept")
            option_id = block["default_option_id"]
        if option_id is None:
            raise Invalid("invalid_submission", "option_id", fields=["option_id"])
        picked = await builder.record_pick(ctx, row["proposal_id"], block_id, option_id, chosen_by="client",
                                           choose_later=later)  # fmt: skip
        option = builder._option(s, option_id)
        await events.record(
            ctx, row["trip_id"], events.OPTION_CHOSEN, proposal_id=row["proposal_id"], bell=True,
            detail={"version": row["version"], "block": block["title"], "option": option["title"],
                    "choose_later": later, "selection_seq": picked["selection_seq"]},
        )  # fmt: skip
        await stages.touch(ctx, row["trip_id"])
        row = await _row(ctx, link)
        return await _page(ctx, keys, link, row)


def _requote(row: Any, now: dt.datetime) -> dict[str, Any]:
    used = int(row["recheck_count_day"]) if row["recheck_day"] == holds.utc_today(now) else 0
    left = max(0, holds.MAX_RECHECKS - used)
    return {"available": left > 0, "rechecks_left": left}


MAX_HOLD_STEPS = 3  # a replay of the request in flight, then (when it held nothing usable) a new request


def _conflict_body(s: dict[str, Any], found: list[dict[str, str]]) -> dict[str, Any]:
    titles = {str(o["option_id"]): o["title"] for o in s["options"]}
    return {
        "detail": "option_conflict",
        "message": "Some of your choices do not go together any more. Please change one of them.",
        "violations": [
            {"kind": v["kind"], "option": titles.get(v["option_id"]), "with": titles.get(v["target_option_id"])}
            for v in found
        ],  # fmt: skip
    }


async def hold(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    booking: BookingClient | None,
    token: str,
    *,
    origin: str | None,
) -> tuple[int, dict[str, Any]]:
    """Hold the AA items of the client's choice (C-04 "Continue", C-12 "Check availability again"). A request whose
    answer was lost is replayed first (:mod:`app.proposals.holds`); when that holds nothing usable for the current
    choice, a new request follows in the same call."""
    check_origin(settings, origin)
    hashed = hash_hex(token)
    for _ in range(MAX_HOLD_STEPS):
        status, body, again = await _hold_step(pool, keys, booking, hashed)
        if not again:
            return status, body
    async with client_link_tx(pool, hashed, purpose=PURPOSE) as (ctx, link):
        return 409, await _busy(ctx, keys, link, await _row(ctx, link))


async def _busy(ctx: AnyContext, keys: KeyProvider, link: Any, row: Any) -> dict[str, Any]:
    return {
        "detail": "hold_in_progress",
        "message": "We are checking availability for this proposal on another screen. Try again in a moment.",
        "page": await _page(ctx, keys, link, row),
    }


async def _hold_step(
    pool: asyncpg.Pool, keys: KeyProvider, booking: BookingClient | None, hashed: str
) -> tuple[int, dict[str, Any], bool]:
    now = _now()
    async with client_link_tx(pool, hashed, purpose=PURPOSE) as (ctx, link):
        row = await _row(ctx, link)
        _require_builder_open(row)
        s, picks = await _builder_state(ctx, row)
        p = await holds.plan(ctx, row, s, picks, now)
        if p.action == "refused":
            # Card R: the 4th re-check of the day: no hold, Booking is not asked; the agent is told.
            await events.record(ctx, row["trip_id"], events.RECHECK_REFUSED, proposal_id=row["proposal_id"],
                                detail={"version": row["version"]}, bell=True)  # fmt: skip
            ctx.audit("proposal_hold", "denied", "proposal", str(row["proposal_id"]), reason="recheck_limit")
            return (
                429,
                {
                    "detail": "recheck_limit",
                    "message": "You checked availability 3 times today. Ask your agent.",
                    "page": await _page(ctx, keys, link, row),
                },
                False,
            )
        if p.action == "conflict":
            ctx.audit("proposal_hold", "denied", "proposal", str(row["proposal_id"]), reason="option_conflict")
            return 409, {**_conflict_body(s, p.conflicts), "page": await _page(ctx, keys, link, row)}, False
        if p.action == "busy":
            return 409, await _busy(ctx, keys, link, row), False
        if p.action in ("none", "reuse"):
            return 200, {"outcome": "not_needed" if p.action == "none" else "held",
                         "page": await _page(ctx, keys, link, row)}, False  # fmt: skip
        tenant_id = link.tenant_id

    async def remember(key: str, quote_id: str, price_hash: str, sha: bytes) -> bool:
        async with client_link_tx(pool, hashed, purpose=PURPOSE) as (rctx, _):
            return await holds.remember(rctx, p.proposal_id, key, quote_id, price_hash, sha)

    if booking is None:
        placed = holds.Placed(outcome="unavailable", code="booking_unavailable", answered=False)
    else:
        placed = await holds.place(booking, tenant_id, p, remember)
    async with client_link_tx(pool, hashed, purpose=PURPOSE) as (ctx, link):
        row = await _row(ctx, link)
        _require_builder_open(row)
        count = await holds.finish(ctx, row, p, placed)
        base = {"version": row["version"], "rechecks_left": p.rechecks_left}
        if placed.outcome == "gone":
            return 0, {}, True  # the replayed request held nothing: a new request follows
        if placed.outcome == "busy":
            return 409, await _busy(ctx, keys, link, row), False
        if placed.outcome == "held":
            if count is not None:
                rehold = count > 1
                await events.record(ctx, row["trip_id"], events.REHOLD if rehold else events.HOLD_PLACED,
                                    proposal_id=row["proposal_id"], bell=rehold,
                                    detail={**base, "hold_count": count, "aa_items": len(p.aa_ids),
                                            "replayed": p.action == "replay"})  # fmt: skip
                ctx.audit("proposal_hold", "allowed", "proposal", str(row["proposal_id"]),
                          reason="rehold" if rehold else "hold")  # fmt: skip
            row = await _row(ctx, link)
            page = await _page(ctx, keys, link, row)
            if p.action == "replay" and page["hold"]["status"] == "stale":
                return 0, {}, True  # recovered a hold of an earlier choice: hold the current one now
            return 200, {"outcome": "held", "page": page}, False
        if placed.outcome == "sold_out":
            # Card E: the option sold out mid-session: the block falls back to its default, the total is recomputed.
            # The current hold was kept (nothing released before a clean quote): it may cover the fallback.
            structure = await builder._structure(ctx, row["proposal_id"])
            titles = {str(o["option_id"]): o["title"] for o in structure["options"]}
            blocks = await holds.sell_out(ctx, row["proposal_id"], placed.sold_out)
            sold = [titles.get(o) for o in placed.sold_out]
            await events.record(ctx, row["trip_id"], events.OPTION_SOLD_OUT, proposal_id=row["proposal_id"],
                                bell=True, detail={**base, "options": sold})  # fmt: skip
            ctx.audit("proposal_hold", "denied", "proposal", str(row["proposal_id"]), reason="option_sold_out")
            row = await _row(ctx, link)
            page = await _page(ctx, keys, link, row)
            default_out = any(
                o["blocked"] and o["blocked"]["reason"] == "sold_out" and o["chosen"]
                for d in page["days"] for b in d["blocks"] for o in b["options"]
            )  # fmt: skip
            if default_out:  # the default itself is gone: only the agent can fix the proposal
                await ctx.conn.execute(HOLD_TASK_SQL, row["trip_id"], row["owner_member_id"])
            return (
                409,
                {
                    "detail": "option_sold_out",
                    "message": "An option you chose is no longer available; we switched back to the included one.",
                    "fallback_blocks": blocks,
                    "ask_agent": default_out,
                    "page": page,
                },
                False,
            )
        row = await _row(ctx, link)  # the hold columns may have been cleared above
        if placed.outcome == "unavailable":
            # F5.5: Booking down: "last checked", Accept disabled, the agent alerted (once an hour).
            if not await events.recent(ctx, row["proposal_id"], events.BOOKING_UNAVAILABLE, OUTAGE_EVENT_EVERY_S):
                await events.record(ctx, row["trip_id"], events.BOOKING_UNAVAILABLE, proposal_id=row["proposal_id"],
                                    bell=True, detail=base)  # fmt: skip
            return (
                503,
                {
                    "detail": "booking_unavailable",
                    "message": "We cannot check availability right now. Please try again later.",
                    "page": await _page(ctx, keys, link, row),
                },
                False,
            )
        code = placed.code or "hold_refused"
        if code == "recheck_limit":
            await events.record(ctx, row["trip_id"], events.RECHECK_REFUSED, proposal_id=row["proposal_id"],
                                detail={**base, "source": "booking"}, bell=True)  # fmt: skip
            return 429, {"detail": "recheck_limit", "message": "Ask your agent.",
                         "page": await _page(ctx, keys, link, row)}, False  # fmt: skip
        await events.record(ctx, row["trip_id"], events.HOLD_FAILED, proposal_id=row["proposal_id"], bell=True,
                            detail={**base, "code": code})  # fmt: skip
        await ctx.conn.execute(HOLD_TASK_SQL, row["trip_id"], row["owner_member_id"])
        return (
            409,
            {
                "detail": "hold_unavailable",
                "message": "We could not hold your places. Your agent will contact you.",
                "page": await _page(ctx, keys, link, row),
            },
            False,
        )


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
        row = await _row(ctx, link)
        if row["status"] == "accepted":
            return {"status": "already_accepted", "accepted_at": _iso(row["accepted_at"])}
        if price != bytes(row["price_hash"]).hex():
            raise Conflict("price_changed", "the proposal changed; read it again")
        if (str(row["terms_doc_id"]) if row["terms_doc_id"] else None) != terms_doc_id:
            raise Conflict("terms_changed", "the terms changed; read them again")
        now = _now()
        total, deposit = Decimal(row["total_amount"]), Decimal(row["deposit_amount"])
        builder_args: tuple[Any, ...] = ()
        due_anchor, due_at = "created", None
        if row["kind"] == "builder":
            if raw.get("rooms_confirmed") is not True:
                raise Invalid(
                    "invalid_submission", "confirm the rooms (or ask for a change)", fields=["rooms_confirmed"]
                )
            s, picks = await _builder_state(ctx, row)
            state = client_page.effective(s, picks)
            shown = client_page.selection_hash(state["choice"], state["later"])
            if raw.get("selection_hash") != shown:
                raise Conflict("choice_changed", "your choices changed (on another device?); check them again",
                               selection_hash=shown)  # fmt: skip
            found = builder._conflicts(s, state["picks"])
            if found:
                # A choice that breaks the agency's rules (e.g. a pick that needed an option which sold out since).
                ctx.audit("proposal_accept", "denied", "proposal", str(row["proposal_id"]), reason="option_conflict")
                body = _conflict_body(s, found)
                raise Conflict("option_conflict", body["message"], violations=body["violations"])
            aa_ids = client_page.aa_chosen(state["choice"], builder._option_map(s))
            running = holds.in_flight(row, now) if aa_ids else None
            if running == "hold_in_progress":
                # Another screen's hold request may release the hold this page shows: never seal it meanwhile.
                ctx.audit("proposal_accept", "denied", "proposal", str(row["proposal_id"]), reason="hold_in_progress")
                raise Conflict("hold_in_progress", "availability is being checked on another screen; try again "
                               "in a moment", requote=_requote(row, now))  # fmt: skip
            if running == "hold_required":
                raise Conflict("hold_required", "check availability first", requote=_requote(row, now))
            hold_box = client_page.hold_state(row, aa_ids, now)
            if hold_box["status"] in ("missing", "stale"):
                raise Conflict("hold_required", "check availability first", requote=_requote(row, now))
            if hold_box["status"] == "expired":
                # Card R: the hold ran out before the client accepted: nothing is accepted; offer a re-check.
                ctx.audit("proposal_accept", "denied", "proposal", str(row["proposal_id"]), reason="hold_expired")
                raise Conflict("hold_expired", "your reservation hold has expired; check availability again",
                               requote=_requote(row, now), expired_at=hold_box["expires_at"])  # fmt: skip
            t = client_page.totals(s, row, state["picks"])
            total, deposit = t["_sell"], t["_deposit"]
            if total <= 0 or deposit <= 0:
                raise Conflict("nothing_to_charge", "ask your agent")
            hold_id = row["hold_id"] if aa_ids else None
            builder_args = (bytes.fromhex(shown), total, deposit, hold_id)
            if aa_ids:
                due_anchor, due_at = "hold_expiry", row["hold_expires_at"]
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        sealed = await vault.encrypt("proposal", "accepted_name_enc", row["proposal_id"], name)
        if builder_args:
            accepted_at = await ctx.conn.fetchval(ACCEPT_BUILDER_SQL, row["proposal_id"], sealed, *builder_args)
        else:
            accepted_at = await ctx.conn.fetchval(ACCEPT_SQL, row["proposal_id"], sealed)
        today = await ctx.conn.fetchval(TODAY_SQL)
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
        await ctx.conn.execute(TASK_SQL, row["trip_id"], row["owner_member_id"], due_anchor, due_at)
        await ctx.conn.execute(SUBMITTED_SQL, link.link_id)
        await stages.touch(ctx, row["trip_id"])
        if row["kind"] == "builder":
            await events.record(ctx, row["trip_id"], events.ACCEPTED, proposal_id=row["proposal_id"], bell=True,
                                detail={"version": row["version"], "total": _money(total)})  # fmt: skip
            if builder_args and builder_args[3] is not None:
                # AA-952: the Booking sync writes the AA lines of the accepted hold set (a job: the client link never
                # writes an AA line, crm-014); asked now so they show in seconds.
                await proposal_lines.nudge(ctx, f"accepted:{row['proposal_id']}")
        ctx.audit("proposal_accept", "allowed", "proposal", str(row["proposal_id"]))
        return {"status": "accepted", "accepted_at": _iso(accepted_at)}


async def change_request(
    pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, token: str, raw: Any, *, origin: str | None
) -> dict[str, Any]:
    """F5.4 / R19: one bundled diff of the client's choice for the agent; ``{"reason": <code>}``."""
    check_origin(settings, origin)
    reason = raw.get("reason") if isinstance(raw, dict) else None
    if reason not in CHANGE_REASONS:
        raise Invalid("invalid_submission", ", ".join(CHANGE_REASONS), fields=["reason"])
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        row = await _row(ctx, link)
        if row["status"] != "sent":
            raise Conflict("already_accepted", "this proposal was accepted already")
        diff: list[dict[str, Any]] = []
        if row["kind"] == "builder":
            s, picks = await _builder_state(ctx, row)
            diff = client_page.choice_diff(s, picks)
        await events.record(ctx, row["trip_id"], events.CHANGE_REQUESTED, proposal_id=row["proposal_id"], bell=True,
                            detail={"version": row["version"], "reason": reason, "choices": diff})  # fmt: skip
        await ctx.conn.execute(CHANGE_TASK_SQL, row["trip_id"], row["owner_member_id"])
        await stages.touch(ctx, row["trip_id"])
        ctx.audit("proposal_change_request", "allowed", "proposal", str(row["proposal_id"]), reason=reason)
        return {"status": "sent", "reason": reason}
