"""One Booking hold set per published version (card CRM-21b, AA-950; design v3 F6.4, §3.5, §5.2, Fix A3, C31, C-12;
migration crm-021). Called by :mod:`app.proposals.public` in steps, so no connection is held while Booking answers
(the AA-947 rule):

1. :func:`plan` (inside the client link's transaction): which AA options the client's choice holds (an agency item
   is never held); a choice that breaks one of the agency's rules is not held (``conflict``); whether the current hold
   still covers it (then nothing is asked); a hold request already in flight (``replay`` when it got as far as its
   quote, ``busy`` while another screen is still quoting). Otherwise a new request: for every hold after the
   version's first (a **re-check**: the hold expired or the client changed an AA pick) the daily limit, at most
   :data:`MAX_RECHECKS` a UTC day per version, counted before Booking is asked, so a 4th re-check is refused without
   a hold ("Ask your agent", card R). The re-check and the request's key (``<ref>:v<version>:a<attempt>``, one per
   new request) are reserved in this transaction (two devices cannot both pass).
2. :func:`place` (no transaction): one quote of every chosen AA line (``proposal_ref`` = ``crm-proposal:<id>``,
   ``proposal_version`` = the version, ``line_ref`` = the option). A line the quote answers ``sold_out`` stops it
   there and the current hold is kept (card E: the caller marks the option sold out and the block falls back to its
   default, which the kept hold may well cover). Then the quote is written down as the request in flight
   (``remember``, a short transaction), the version's old hold released (Booking keeps one active set per
   (proposal_ref, version)), and ``POST /holds`` with the request's key. A ``replay`` sends the remembered key and
   quote again: Booking answers with the hold it made when the first answer was lost (a timeout after its commit),
   or makes it now.
3. :func:`finish` (a new transaction): a hold goes on the version row (id, quote, expiry, the AA choice it covers),
   with the count; when the old hold was released and no new one came, the hold columns are cleared (the page then
   shows "check availability", never a hold Booking no longer has); the request in flight is cleared once Booking
   answered, kept when the answer was lost (the next request replays it).

No personal data goes to Booking: counts by age band and the agent's rooms only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.proposals import builder, client_page, rules
from app.proposals.aa_options import proposal_ref
from app.tenancy import AnyContext

MAX_RECHECKS = client_page.MAX_RECHECKS
# A request in flight that has not reached its quote after this long died (the process stopped): a new one replaces it.
PENDING_STALE = dt.timedelta(minutes=2)

RESERVE_SQL = "UPDATE crm.proposal SET recheck_day = $2, recheck_count_day = $3 WHERE proposal_id = $1"
ATTEMPT_SQL = """
UPDATE crm.proposal SET hold_attempt = $2, hold_pending_key = $3, hold_pending_at = now(), hold_pending_quote_id = NULL,
       hold_pending_price_hash = NULL, hold_pending_selection_sha256 = NULL
 WHERE proposal_id = $1
"""
REMEMBER_SQL = """
UPDATE crm.proposal SET hold_pending_quote_id = $3, hold_pending_price_hash = $4, hold_pending_selection_sha256 = $5
 WHERE proposal_id = $1 AND hold_pending_key = $2
RETURNING proposal_id
"""
FORGET_SQL = """
UPDATE crm.proposal SET hold_pending_key = NULL, hold_pending_at = NULL, hold_pending_quote_id = NULL,
       hold_pending_price_hash = NULL, hold_pending_selection_sha256 = NULL
 WHERE proposal_id = $1 AND hold_pending_key = $2
"""
STORE_SQL = """
UPDATE crm.proposal SET hold_set_ref = $2, hold_id = $3, hold_quote_id = $4, hold_expires_at = $5,
       hold_selection_sha256 = $6, hold_placed_at = now(), hold_count = hold_count + 1
 WHERE proposal_id = $1
RETURNING hold_count
"""
CLEAR_HOLD_SQL = """
UPDATE crm.proposal SET hold_id = NULL, hold_quote_id = NULL, hold_expires_at = NULL, hold_selection_sha256 = NULL,
       hold_placed_at = NULL
 WHERE proposal_id = $1 AND hold_id = $2
"""
SOLD_OUT_SQL = """
UPDATE crm.option SET availability = 'sold_out', availability_checked_at = now()
 WHERE option_id = ANY($1::uuid[]) AND availability <> 'sold_out'
"""
DROP_PICKS_SQL = """
DELETE FROM crm.selection WHERE proposal_id = $1 AND option_id = ANY($2::uuid[]) RETURNING option_block_id
"""
PICK_SEQ_SQL = "UPDATE crm.proposal SET selection_seq = selection_seq + 1 WHERE proposal_id = $1"

# remember(key, quote_id, price_hash, selection_sha256) -> False when the request is no longer this version's own
Remember = Callable[[str, str, str, bytes], Awaitable[bool]]


@dataclass
class Plan:
    # none (nothing to hold) | reuse (the hold covers the choice) | refused (re-check limit) | conflict (a rule break)
    # | busy (another screen is quoting) | replay (the request in flight, again) | place (a new request)
    action: str
    proposal_id: uuid.UUID
    version: int
    aa_ids: list[str] = field(default_factory=list)
    body: dict[str, Any] | None = None
    release_hold_id: str | None = None
    idempotency_key: str | None = None
    selection_sha256: bytes | None = None  # the AA choice the request holds
    replay_body: dict[str, Any] | None = None
    rehold: bool = False
    rechecks_left: int = MAX_RECHECKS
    conflicts: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Placed:
    outcome: str  # held | sold_out | refused | unavailable | busy | gone (a replay Booking has nothing for)
    hold: contract.Hold | None = None
    quote_id: str | None = None
    sold_out: list[str] = field(default_factory=list)
    code: str | None = None
    released: str | None = None  # the old hold Booking released on the way
    answered: bool = True  # False: the answer of POST /holds was lost (the request stays in flight)


def utc_today(now: dt.datetime) -> dt.date:
    return now.astimezone(dt.UTC).date()


def _lines(row: Any, s: dict[str, Any], aa_ids: list[str]) -> list[dict[str, Any]]:
    days = {d["proposal_day_id"]: d for d in s["days"]}
    blocks = {b["option_block_id"]: b for b in s["blocks"]}
    options = builder._option_map(s)
    pax = rules.room_pax(builder._jsonb(row["room_config"]), int(row["party_adults"]), row["party_child_ages"] or [])
    out = []
    for oid in aa_ids:
        o = options[oid]
        date = builder._day_date(row["starts_on"], days[blocks[o["option_block_id"]]["proposal_day_id"]]["day_no"])
        item: dict[str, Any] = {
            "line_ref": oid,
            "component_id": o["aa_component_id"],
            "service_date": date.isoformat() if date else None,
            "pax": {"adult": int(pax["adult"]), "child": int(pax["child"]), "infant": int(pax["infant"])},
        }
        if o["aa_departure_id"]:
            item["departure_id"] = o["aa_departure_id"]
        if o["aa_nights"]:
            item["nights"] = int(o["aa_nights"])
        if pax.get("rooms"):
            item["rooms"] = pax["rooms"]
        out.append(item)
    return out


async def plan(ctx: AnyContext, row: Any, s: dict[str, Any], pick_rows: list[dict[str, Any]], now: dt.datetime) -> Plan:
    """Step 1 (the row is locked by the caller)."""
    state = client_page.effective(s, pick_rows)
    aa_ids = client_page.aa_chosen(state["choice"], builder._option_map(s))
    out = Plan(action="none", proposal_id=row["proposal_id"], version=int(row["version"]), aa_ids=aa_ids)
    if not aa_ids:
        return out
    out.conflicts = builder._conflicts(s, state["picks"])
    if out.conflicts:
        out.action = "conflict"  # e.g. a pick that needed an option which sold out: the client fixes the choice first
        return out
    sha = client_page.aa_hash(aa_ids)
    live = row["hold_id"] is not None and row["hold_expires_at"] > now
    if live and bytes(row["hold_selection_sha256"]) == sha:
        out.action = "reuse"
        return out
    ref = proposal_ref(row["proposal_id"])
    if row["hold_pending_key"] is not None:
        if row["hold_pending_quote_id"] is not None:
            # The last request got as far as POST /holds and its answer never came: send it again, same key and quote.
            out.action = "replay"
            out.idempotency_key = row["hold_pending_key"]
            out.selection_sha256 = bytes(row["hold_pending_selection_sha256"])
            out.replay_body = {"quote_id": row["hold_pending_quote_id"], "price_hash": row["hold_pending_price_hash"],
                               "proposal_ref": ref, "proposal_version": int(row["version"])}  # fmt: skip
            return out
        if row["hold_pending_at"] > now - PENDING_STALE:
            out.action = "busy"  # another screen is quoting this version right now
            return out
    today = utc_today(now)
    used = int(row["recheck_count_day"]) if row["recheck_day"] == today else 0
    out.rehold = int(row["hold_count"]) >= 1
    if out.rehold:
        if used >= MAX_RECHECKS:
            out.action = "refused"
            out.rechecks_left = 0
            return out
        await ctx.conn.execute(RESERVE_SQL, row["proposal_id"], today, used + 1)
        out.rechecks_left = MAX_RECHECKS - used - 1
    out.action = "place"
    if live:
        out.release_hold_id = row["hold_id"]  # stale: Booking holds one set per version
    out.body = {
        "proposal_ref": ref,
        "proposal_version": int(row["version"]),
        "currency": row["currency"],
        "lines": _lines(row, s, aa_ids),
    }
    out.selection_sha256 = sha
    attempt = int(row["hold_attempt"]) + 1  # a fresh key for every new request (Booking: one key, one quote)
    out.idempotency_key = f"{ref}:v{row['version']}:a{attempt}"
    await ctx.conn.execute(ATTEMPT_SQL, row["proposal_id"], attempt, out.idempotency_key)
    return out


def _refused(exc: BookingRefusal, released: str | None) -> Placed:
    if exc.code == "rehold_limit":
        return Placed(outcome="refused", code="recheck_limit", released=released)
    if exc.code == "sold_out":
        return Placed(outcome="refused", code="hold_sold_out", released=released)
    code = exc.code if exc.code in ("tour_not_served", "price_changed", "invalid") else "hold_refused"
    return Placed(outcome="refused", code=code, released=released)


async def _post(client: BookingClient, tenant_id: uuid.UUID, body: dict[str, Any], key: str,
                released: str | None, quote_id: str) -> Placed:  # fmt: skip
    try:
        hold = contract.hold(await client.hold(tenant_id, body, idempotency_key=key))
    except BookingRefusal as exc:
        return _refused(exc, released)
    except (BookingUnavailable, contract.ContractError):
        # Booking may have made the hold: the request stays in flight and the next one replays it.
        return Placed(outcome="unavailable", code="booking_unavailable", released=released, answered=False)
    if hold.status != "active":
        return Placed(outcome="refused", code="hold_refused", released=released)
    return Placed(outcome="held", hold=hold, quote_id=quote_id, released=released)


async def place(client: BookingClient, tenant_id: uuid.UUID, p: Plan, remember: Remember) -> Placed:
    """Step 2: Booking only (no connection held but ``remember``'s short transaction)."""
    if p.action == "replay":
        assert p.replay_body is not None and p.idempotency_key is not None  # noqa: S101 — plan() sets both
        placed = await _post(client, tenant_id, p.replay_body, p.idempotency_key, None, p.replay_body["quote_id"])
        if placed.outcome == "refused" and placed.code != "recheck_limit":
            # Nothing was held under that key and the quote cannot be held now (expired, ...): a new request follows.
            return Placed(outcome="gone", code=placed.code)
        return placed
    assert p.body is not None and p.idempotency_key is not None and p.selection_sha256 is not None  # noqa: S101
    try:
        data = await client.quote(tenant_id, p.body)
        lines = {oid: contract.quote_line(data, oid) for oid in p.aa_ids}
    except BookingRefusal as exc:
        return _refused(exc, None)
    except (BookingUnavailable, contract.ContractError):
        return Placed(outcome="unavailable", code="booking_unavailable")
    sold_out = [oid for oid, line in lines.items() if line.availability == "sold_out"]
    if sold_out:
        return Placed(outcome="sold_out", sold_out=sold_out)  # the current hold is kept: nothing was released
    first = next(iter(lines.values()))
    if not await remember(p.idempotency_key, first.quote_id, first.price_hash, p.selection_sha256):
        return Placed(outcome="busy")  # another screen started a newer request for this version meanwhile
    released = None
    if p.release_hold_id is not None:
        try:
            await client.release(tenant_id, p.release_hold_id)
        except BookingRefusal:
            pass  # gone, expired or confirmed: nothing to release
        except BookingUnavailable:
            return Placed(outcome="unavailable", code="booking_unavailable")
        released = p.release_hold_id
    body = {
        "quote_id": first.quote_id,
        "price_hash": first.price_hash,
        "proposal_ref": p.body["proposal_ref"],
        "proposal_version": p.body["proposal_version"],
    }
    return await _post(client, tenant_id, body, p.idempotency_key, released, first.quote_id)


async def remember(ctx: AnyContext, proposal_id: uuid.UUID, key: str, quote_id: str, price_hash: str,
                   sha: bytes) -> bool:  # fmt: skip
    """The quote of the request in flight, written before ``POST /holds`` (so a lost answer can be replayed)."""
    return await ctx.conn.fetchval(REMEMBER_SQL, proposal_id, key, quote_id, price_hash, sha) is not None


async def finish(ctx: AnyContext, row: Any, p: Plan, placed: Placed) -> int | None:
    """Step 3 (``row`` locked again by the caller): the version row after Booking's answer. Returns the version's hold
    count when a new hold went on the row, else None."""
    count = None
    if placed.outcome == "held":
        assert placed.hold is not None and p.selection_sha256 is not None  # noqa: S101
        if row["hold_id"] != placed.hold.hold_id:  # a replay another screen stored already adds nothing
            count = int(await ctx.conn.fetchval(STORE_SQL, p.proposal_id, proposal_ref(p.proposal_id),
                                                placed.hold.hold_id, placed.quote_id, placed.hold.expires_at,
                                                p.selection_sha256))  # fmt: skip
    elif placed.released is not None:
        # Booking let the old hold go and no new one came: the row must not show a hold Booking no longer has.
        await ctx.conn.execute(CLEAR_HOLD_SQL, p.proposal_id, placed.released)
    if placed.answered and placed.outcome != "busy" and p.idempotency_key is not None:
        await ctx.conn.execute(FORGET_SQL, p.proposal_id, p.idempotency_key)
    return count


async def sell_out(ctx: AnyContext, proposal_id: uuid.UUID, option_ids: list[str]) -> list[str]:
    """Card E: the options Booking answered sold out are marked so (crm-019 lets a sent version change availability
    only) and the client's picks on them are dropped: those blocks fall back to their default."""
    ids = [uuid.UUID(o) for o in option_ids]
    await ctx.conn.execute(SOLD_OUT_SQL, ids)
    blocks = [str(r["option_block_id"]) for r in await ctx.conn.fetch(DROP_PICKS_SQL, proposal_id, ids)]
    if blocks:
        await ctx.conn.execute(PICK_SEQ_SQL, proposal_id)
    return blocks
