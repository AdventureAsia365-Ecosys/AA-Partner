"""The builder's "AA add-on" drawer (AA-1152; S285 advisory PR-29 T6, root ADR 0019 b6: AA add-ons come from AA's
catalogue, an agency never writes one).

``GET /proposals/{id}/blocks/{block}/aa-addons`` lists, for the block's day (its date and the party), the AA add-ons
Booking sells that day (booking.b2b.v1 1.11 ``GET /add-ons``) at a place of the AA tour on that day (S291 follow-up:
the TripPlanner day cards' rule, :mod:`app.proposals.aa_addon_places`), the ones at the day's place first (the day's
place text matched against the add-on's CIS place names), each with the agency's suggested price per traveller (its own
markup on AA's B2B price, else AA's RRP; then its price rules for the date; rounded up to its step:
:func:`app.proposals.rounding.from_price`) and, for a role that sees margins, the B2B price per traveller.

``POST /proposals/{id}/blocks/{block}/aa-addons`` adds the chosen one as an AA option: the same two steps as an AA item
(:func:`app.proposals.builder.aa_plan` in the request's transaction, the live quote outside it,
:func:`app.proposals.builder.add_aa_option`), with AA's name of the add-on (read again from Booking, never the
browser's) and its RRP as the option's "from" price. An AA option belongs to an AA trip (``crm.option`` keeps the AA
tour key): the add-on takes the AA tour nearest that day (:func:`app.proposals.aa_addon_places.of_block`), else the
proposal's first AA tour; a proposal without an AA tour is refused (409 ``aa_addon_needs_tour``: add the AA tour
first). An add-on Booking does not sell that day is 409 ``aa_addon_not_offered``; one sold at none of the tour day's
places is 409 ``aa_addon_not_on_tour`` (the drawer never offers it; the API refuses a hand-made request too).

"Refresh AA prices" (:func:`refresh_rrp`) reads AA's RRP of each add-on option again (one add-on list per day), so the
suggestion's RRP fallback follows AA's price; an add-on Booking no longer lists (or a failed list) keeps its last RRP.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from app.booking import addons as booking_addons
from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, Invalid, Unavailable
from app.margin import sees_margin
from app.proposals import aa_addon_places, aa_options, builder, pricing, rounding
from app.tenancy import TenantContext
from app.vault import Vault

# The AA tour of the block's day first, else the proposal's first AA tour (an add-on belongs to an AA trip).
TOUR_SQL = """
SELECT o.aa_tour_source_id
  FROM crm.option o JOIN crm.option_block b ON b.option_block_id = o.option_block_id
 WHERE o.proposal_id = $1 AND o.source = 'aa' AND o.deleted_at IS NULL AND o.aa_tour_source_id IS NOT NULL
 ORDER BY (b.proposal_day_id = (SELECT x.proposal_day_id FROM crm.option_block x WHERE x.option_block_id = $2)) DESC,
          o.created_at, o.option_id
 LIMIT 1
"""


def _unavailable() -> Unavailable:
    return Unavailable("booking_unavailable", "AA cannot be reached right now; AA items cannot be added")


def matches(place: str | None, addon: booking_addons.Addon) -> bool:
    """The day's place text names one of the add-on's places (either contains the other, any case)."""
    text = (place or "").strip().casefold()
    if not text:
        return False
    names = [(p.name or "").casefold() for p in addon.places]
    return any(n and (n in text or text in n) for n in names)


async def day_of(ctx: TenantContext, proposal_id: uuid.UUID, block_id: uuid.UUID) -> dict[str, Any]:
    """The block's day as the drawer needs it: date (None without a day 1 date), party, currency, place, AA tour, the
    tour day's places (CIS destination ids) and the agency's pricing context (markups, rules, today, rounding step)."""
    row = await builder._lock(ctx, proposal_id, None)  # noqa: SLF001 — the builder's own reads (one module split)
    s = await builder._structure(ctx, proposal_id)  # noqa: SLF001
    block = builder._block(s, block_id)  # noqa: SLF001
    day = await builder._day(s, block["proposal_day_id"])  # noqa: SLF001
    places = await aa_addon_places.of_block(ctx, proposal_id, block_id, TOUR_SQL)
    return {
        "date": builder._day_date(row["starts_on"], day["day_no"]),  # noqa: SLF001
        "party": builder._party(row),  # noqa: SLF001
        "currency": row["currency"],
        "place": day.get("place"),
        "tour_source_id": places.tour_source_id,
        "tour_places": places.places,
        "rctx": await builder._retail_ctx(ctx, {"options": [{"source": "aa"}]}, row),  # noqa: SLF001
        "sees_margin": sees_margin(ctx.member_role),
    }


async def offered(client: BookingClient, tenant_id: uuid.UUID, day: dict[str, Any]) -> list[booking_addons.Addon]:
    try:
        return await booking_addons.load(
            client, tenant_id, currency=day["currency"], pax=max(day["party"], 1), on=day["date"]
        )
    except BookingRefusal as exc:
        if exc.code in ("unknown_partner", "forbidden"):
            raise Conflict("aa_partner_not_active", "AA has not activated your agency for AA products yet") from None
        raise Invalid("aa_addons_refused", f"AA refused the add-on list ({exc.code})", aa_code=exc.code) from None
    except (BookingUnavailable, contract.ContractError):
        raise _unavailable() from None


async def day_offered(client: BookingClient, tenant_id: uuid.UUID, day: dict[str, Any]) -> list[booking_addons.Addon]:
    """The add-ons of the day sold at a place of the AA tour that day (no tour place: none, Booking is not asked)."""
    allowed = aa_addon_places.DayPlaces(day["tour_source_id"], day["tour_places"])
    if not allowed.places:
        return []
    return [a for a in await offered(client, tenant_id, day) if allowed.allows([p.destination_id for p in a.places])]


def _suggested(addon: booking_addons.Addon, day: dict[str, Any]) -> tuple[Decimal, str] | None:
    rctx = day["rctx"]
    pct = Decimal("0")
    if day["date"] is not None:
        pct, _ = pricing.adjustment(rctx["rules"], kind=addon.kind, tour_source_id=day["tour_source_id"],
                                    service_date=day["date"], pax=day["party"], today=rctx["today"])  # fmt: skip
    markup = rctx["markups"].get(addon.kind)
    return rounding.from_price(addon.b2b_per_person, addon.rrp_per_person, markup, rctx["round_step"], pct)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(rounding.CENT), "f")


def item(addon: booking_addons.Addon, day: dict[str, Any]) -> dict[str, Any]:
    suggested = _suggested(addon, day)
    # A markup suggestion is derived from the B2B price: shown to margin roles only, like the builder's (AA-1030).
    shown = suggested is not None and (suggested[1] != "agency_markup" or day["sees_margin"])
    return {
        "component_id": addon.component_id,
        "kind": addon.kind,
        "title": addon.title,
        "unit": addon.unit,
        "duration_minutes": addon.duration_minutes,
        "min_pax": addon.min_pax,
        "operating_weekdays": list(addon.operating_weekdays),
        "start_times": list(addon.start_times),
        "first_date": addon.first_date.isoformat(),
        "places": [{"destination_id": p.destination_id, "role": p.role, "name": p.name} for p in addon.places],
        "at_day_place": matches(day["place"], addon),
        "suggested_per_person": _money(suggested[0]) if shown and suggested else None,
        "suggested_basis": suggested[1] if shown and suggested else None,
        "b2b_per_person": _money(addon.b2b_per_person) if day["sees_margin"] else None,
    }


def listing(addons: list[booking_addons.Addon], day: dict[str, Any]) -> dict[str, Any]:
    items = sorted((item(a, day) for a in addons), key=lambda i: (not i["at_day_place"], i["title"].casefold()))
    date: dt.date | None = day["date"]
    return {
        "date": date.isoformat() if date else None,
        "party": day["party"],
        "currency": day["currency"],
        "place": day["place"],
        "needs_tour": day["tour_source_id"] is None,
        # An AA tour is there but its day has no known place yet (CIS has not resolved it): nothing can be offered.
        "no_tour_place": day["tour_source_id"] is not None and not day.get("tour_places"),
        "items": items,
    }


async def plan(
    ctx: TenantContext, proposal_id: uuid.UUID, seq: int, block_id: uuid.UUID, component_id: str, kind: str
) -> dict[str, Any]:
    """Step 1 (in the transaction): the AA item plan for the add-on, on the AA tour of the trip nearest the day, with
    that tour day's places (checked against Booking's places of the add-on in step 2)."""
    places = await aa_addon_places.of_block(ctx, proposal_id, block_id, TOUR_SQL)
    if places.tour_source_id is None:
        raise Conflict("aa_addon_needs_tour", "add the AA tour first: AA add-ons are sold with an AA trip")
    body = {"tour_source_id": places.tour_source_id, "component_id": component_id, "component_kind": kind}
    the_plan = await builder.aa_plan(ctx, proposal_id, seq, block_id, body)
    return {**the_plan, "addon": True, "tour_places": sorted(places.places)}


async def quote(
    client: BookingClient, tenant_id: uuid.UUID, the_plan: dict[str, Any]
) -> tuple[dict[str, Any], aa_options.AaQuote]:
    """Step 2 (no transaction): AA's name of the add-on that day (Booking's, never the browser's), a check that it is
    sold at a place of the tour day (409 ``aa_addon_not_on_tour``), then the live quote, with AA's RRP as the
    option's "from" price."""
    day = {"currency": the_plan["currency"], "party": the_plan["party"],
           "date": dt.date.fromisoformat(the_plan["service_date"])}  # fmt: skip
    found = next((a for a in await offered(client, tenant_id, day) if a.component_id == the_plan["component_id"]), None)
    if found is None:
        raise Conflict("aa_addon_not_offered", "AA does not sell this add-on on that day")
    allowed = aa_addon_places.DayPlaces(the_plan["tour_source_id"], frozenset(the_plan.get("tour_places") or ()))
    if not allowed.allows([p.destination_id for p in found.places]):
        raise Conflict(
            "aa_addon_not_on_tour",
            "this add-on is not sold at a place of the AA tour on that day; pick one from the day's list",
        )
    answer = await aa_options.quote(client, tenant_id, the_plan)
    return {**the_plan, "title": found.title}, dataclasses.replace(answer, from_price=found.rrp_per_person)


async def refresh_rrp(
    client: BookingClient, tenant_id: uuid.UUID, answers: list[tuple[dict[str, Any], aa_options.AaQuote]]
) -> list[tuple[dict[str, Any], aa_options.AaQuote]]:
    """The refresh answers with AA's current RRP on each add-on option (an AA item other than a tour that carries a
    "from" price: only add-ons do; :func:`app.proposals.aa_options.quote` gives a tour's only). One add-on list per day
    and party; the RRP is a suggestion only, so an add-on Booking no longer lists, or a list it refuses or cannot give,
    keeps the stored RRP (never fails the refresh, never drops the suggestion's fallback)."""
    lists: dict[tuple[str, int], dict[str, booking_addons.Addon] | None] = {}
    out = []
    for the_plan, answer in answers:
        if the_plan["component_kind"] == "trip_plan" or the_plan.get("from_price") is None or answer.quote_id is None:
            out.append((the_plan, answer))
            continue
        key = (the_plan["service_date"], int(the_plan["party"]))
        if key not in lists:
            day = {"currency": the_plan["currency"], "party": key[1], "date": dt.date.fromisoformat(key[0])}
            try:
                lists[key] = {a.component_id: a for a in await offered(client, tenant_id, day)}
            except (Conflict, Invalid, Unavailable):
                lists[key] = None
        found = (lists[key] or {}).get(the_plan["component_id"])
        rrp = found.rrp_per_person if found is not None else None
        out.append((the_plan, dataclasses.replace(answer, from_price=rrp or Decimal(the_plan["from_price"]))))
    return out


async def add(
    ctx: TenantContext, vault: Vault, the_plan: dict[str, Any], answer: aa_options.AaQuote, body: dict[str, Any]
) -> dict[str, Any]:
    """Step 3 (a new transaction): the AA option (:func:`app.proposals.builder.add_aa_option`)."""
    return await builder.add_aa_option(ctx, vault, the_plan, answer, body)
