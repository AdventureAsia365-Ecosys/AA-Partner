"""One Booking hold set per published version (card CRM-21b, AA-950; design v3 F6.4, §3.5, §5.2, Fix A3, C31, C-12;
migration crm-021). Called by :mod:`app.proposals.public` in three steps, so no connection is held while Booking
answers (the AA-947 rule):

1. :func:`plan` (inside the client link's transaction): which AA options the client's choice holds (an agency item
   is never held), whether the current hold still covers them (then nothing is asked), and — for every hold after the
   version's first (a **re-check**: the hold expired or the client changed an AA pick) — the daily limit: at most
   :data:`MAX_RECHECKS` a UTC day per version, counted before Booking is asked, so a 4th re-check is refused without
   a hold ("Ask your agent", card R). The re-check is reserved in this transaction (two devices cannot both pass).
2. :func:`place` (no transaction): release the old hold of this version when it no longer fits (Booking keeps one
   active set per (proposal_ref, version)), one quote of every chosen AA line (``proposal_ref`` =
   ``crm-proposal:<id>``, ``proposal_version`` = the version, ``line_ref`` = the option), then ``POST /holds`` with a
   stable ``Idempotency-Key`` (``<ref>:v<version>:h<n>``). A line the quote answers ``sold_out`` stops it before any
   hold (card E: the caller marks the option sold out and the block falls back to its default).
3. :func:`store` (a new transaction): the hold on the version row (id, quote, expiry, the AA choice it covers), the
   count, and an event: the first hold quietly, every re-hold rings the trip owner's bell (Fix A3).

No personal data goes to Booking: counts by age band and the agent's rooms only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.proposals import builder, client_page, rules
from app.proposals.aa_options import proposal_ref
from app.tenancy import AnyContext

MAX_RECHECKS = client_page.MAX_RECHECKS

RESERVE_SQL = "UPDATE crm.proposal SET recheck_day = $2, recheck_count_day = $3 WHERE proposal_id = $1"
STORE_SQL = """
UPDATE crm.proposal SET hold_set_ref = $2, hold_id = $3, hold_quote_id = $4, hold_expires_at = $5,
       hold_selection_sha256 = $6, hold_placed_at = now(), hold_count = hold_count + 1
 WHERE proposal_id = $1
RETURNING hold_count
"""
SOLD_OUT_SQL = """
UPDATE crm.option SET availability = 'sold_out', availability_checked_at = now()
 WHERE option_id = ANY($1::uuid[]) AND availability <> 'sold_out'
"""
DROP_PICKS_SQL = """
DELETE FROM crm.selection WHERE proposal_id = $1 AND option_id = ANY($2::uuid[]) RETURNING option_block_id
"""
PICK_SEQ_SQL = "UPDATE crm.proposal SET selection_seq = selection_seq + 1 WHERE proposal_id = $1"


@dataclass
class Plan:
    action: str  # none (nothing to hold) | reuse (the hold covers the choice) | refused (re-check limit) | place
    proposal_id: uuid.UUID
    version: int
    aa_ids: list[str] = field(default_factory=list)
    body: dict[str, Any] | None = None
    release_hold_id: str | None = None
    idempotency_key: str | None = None
    rehold: bool = False
    rechecks_left: int = MAX_RECHECKS


@dataclass
class Placed:
    outcome: str  # held | sold_out | refused | unavailable
    hold: contract.Hold | None = None
    quote_id: str | None = None
    sold_out: list[str] = field(default_factory=list)
    code: str | None = None


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
    covers = row["hold_id"] is not None and bytes(row["hold_selection_sha256"]) == client_page.aa_hash(aa_ids)
    if covers and row["hold_expires_at"] > now:
        out.action = "reuse"
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
    if row["hold_id"] is not None and row["hold_expires_at"] > now:
        out.release_hold_id = row["hold_id"]  # stale: Booking holds one set per version
    out.body = {
        "proposal_ref": proposal_ref(row["proposal_id"]),
        "proposal_version": int(row["version"]),
        "currency": row["currency"],
        "lines": _lines(row, s, aa_ids),
    }
    out.idempotency_key = f"{proposal_ref(row['proposal_id'])}:v{row['version']}:h{int(row['hold_count']) + 1}"
    return out


async def place(client: BookingClient, tenant_id: uuid.UUID, p: Plan) -> Placed:
    """Step 2: Booking only (no connection held)."""
    assert p.body is not None and p.idempotency_key is not None  # noqa: S101 — plan() sets both for "place"
    try:
        if p.release_hold_id is not None:
            try:
                await client.release(tenant_id, p.release_hold_id)
            except BookingRefusal:
                pass  # gone, expired or confirmed: nothing to release
        data = await client.quote(tenant_id, p.body)
        lines = {oid: contract.quote_line(data, oid) for oid in p.aa_ids}
        sold_out = [oid for oid, line in lines.items() if line.availability == "sold_out"]
        if sold_out:
            return Placed(outcome="sold_out", sold_out=sold_out)
        first = next(iter(lines.values()))
        body = {
            "quote_id": first.quote_id,
            "price_hash": first.price_hash,
            "proposal_ref": p.body["proposal_ref"],
            "proposal_version": p.body["proposal_version"],
        }
        hold = contract.hold(await client.hold(tenant_id, body, idempotency_key=p.idempotency_key))
    except BookingRefusal as exc:
        if exc.code == "rehold_limit":
            return Placed(outcome="refused", code="recheck_limit")
        if exc.code == "sold_out":
            return Placed(outcome="refused", code="hold_sold_out")
        return Placed(outcome="refused", code=exc.code if exc.code in ("tour_not_served", "price_changed",
                                                                       "invalid") else "hold_refused")  # fmt: skip
    except (BookingUnavailable, contract.ContractError):
        return Placed(outcome="unavailable", code="booking_unavailable")
    if hold.status != "active":
        return Placed(outcome="refused", code="hold_refused")
    return Placed(outcome="held", hold=hold, quote_id=first.quote_id)


async def store(ctx: AnyContext, p: Plan, placed: Placed) -> int:
    """Step 3: the hold on the version row; returns the version's hold count."""
    assert placed.hold is not None  # noqa: S101
    return int(
        await ctx.conn.fetchval(
            STORE_SQL,
            p.proposal_id,
            proposal_ref(p.proposal_id),
            placed.hold.hold_id,
            placed.quote_id,
            placed.hold.expires_at,
            client_page.aa_hash(p.aa_ids),
        )  # fmt: skip
    )


async def sell_out(ctx: AnyContext, proposal_id: uuid.UUID, option_ids: list[str]) -> list[str]:
    """Card E: the options Booking answered sold out are marked so (crm-019 lets a sent version change availability
    only) and the client's picks on them are dropped: those blocks fall back to their default."""
    ids = [uuid.UUID(o) for o in option_ids]
    await ctx.conn.execute(SOLD_OUT_SQL, ids)
    blocks = [str(r["option_block_id"]) for r in await ctx.conn.fetch(DROP_PICKS_SQL, proposal_id, ids)]
    if blocks:
        await ctx.conn.execute(PICK_SEQ_SQL, proposal_id)
    return blocks
