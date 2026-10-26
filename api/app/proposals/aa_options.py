"""The live Booking quote behind an AA item of the builder (card CRM-21a, AA-949; design F3.2 "AA items come from the
inventory drawer, showing B2B price, sell, margin and availability"; WF-4 "If AA is offline the drawer says so and AA
adds are disabled").

Called after the request's transaction closed (no connection held while Booking answers), between
:func:`app.proposals.builder.aa_plan` and :func:`~app.proposals.builder.add_aa_option` /
:func:`~app.proposals.builder.apply_refresh`. One quote per AA item: ``proposal_ref = crm-proposal:<id>``,
``proposal_version`` = the version, ``line_ref`` = the option id — the reference CRM-21b holds one set per version
with. A builder quote is a price check: no hold, no money row, nothing in the outage queue (an AA *line* is still made
only by a quote / hold / feed, AA-947, once the client accepts).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, Invalid, Unavailable

MAX_REFRESH = 40


@dataclass(frozen=True)
class AaQuote:
    quote_id: str | None  # None: Booking answered sold out without a quote (refresh only)
    b2b_price: Decimal | None
    availability: str
    valid_until: dt.datetime
    from_price: Decimal | None


def proposal_ref(proposal_id: uuid.UUID | str) -> str:
    return f"crm-proposal:{proposal_id}"


def quote_body(plan: dict[str, Any]) -> dict[str, Any]:
    pax = plan["pax"]
    item: dict[str, Any] = {
        "line_ref": plan["option_id"],
        "component_id": plan["component_id"],
        "service_date": plan["service_date"],
        "pax": {"adult": int(pax["adult"]), "child": int(pax["child"]), "infant": int(pax["infant"])},
    }
    if plan.get("departure_id"):
        item["departure_id"] = plan["departure_id"]
    if plan.get("nights"):
        item["nights"] = int(plan["nights"])
    if pax.get("rooms"):
        item["rooms"] = pax["rooms"]
    return {
        "proposal_ref": proposal_ref(plan["proposal_id"]),
        "proposal_version": int(plan["version"]),
        "currency": plan["currency"],
        "lines": [item],
    }


def _refusal(exc: BookingRefusal) -> Exception:
    if exc.code in ("unknown_partner", "forbidden"):
        return Conflict("aa_partner_not_active", "AA has not activated your agency for AA products yet")
    if exc.code == "sold_out":
        return Conflict("aa_sold_out", "AA has no space for this item on that date")
    if exc.code == "tour_not_served":
        return Conflict("aa_tour_not_served", "AA no longer offers this tour")
    return Invalid("aa_quote_refused", f"AA refused the quote ({exc.code})", aa_code=exc.code)


async def quote(client: BookingClient, tenant_id: uuid.UUID, plan: dict[str, Any]) -> AaQuote:
    """The live price and availability of one AA item. Booking down: 503 ``booking_unavailable`` (nothing stored)."""
    try:
        data = await client.quote(tenant_id, quote_body(plan))
        line = contract.quote_line(data, plan["option_id"])
    except BookingRefusal as exc:
        raise _refusal(exc) from None
    except (BookingUnavailable, contract.ContractError):
        raise Unavailable("booking_unavailable", "AA cannot be reached right now; AA items cannot be added") from None
    from_price = None
    if plan["component_kind"] == "trip_plan":
        try:  # the "from" price is a warning only: its absence never fails the quote (ADR 0005a decision 1)
            from_price = contract.from_price(
                await client.price_parity(tenant_id, plan["tour_source_id"], plan["currency"]), plan["currency"]
            )
        except (BookingUnavailable, BookingRefusal, contract.ContractError):
            from_price = None
    return AaQuote(
        quote_id=line.quote_id,
        b2b_price=line.b2b_price,
        availability=line.availability,
        valid_until=line.valid_until,
        from_price=from_price,
    )


async def refresh(
    client: BookingClient, tenant_id: uuid.UUID, plans: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], AaQuote]], list[dict[str, str]]]:
    """Quote the AA items again, at most :data:`MAX_REFRESH` per run, in the order given (the builder sends the oldest
    quote first, so another run quotes the rest). Nothing is dropped silently: an item past the limit is reported as
    ``refresh_limit``, an item on a day without a date as ``day_has_no_date``, an item Booking refuses with its code
    (each left as it was). Booking down stops the run (503, nothing stored)."""
    answers: list[tuple[dict[str, Any], AaQuote]] = []
    failed: list[dict[str, str]] = []
    asked = 0
    for plan in plans:
        if plan.get("service_date") is None:
            failed.append({"option_id": plan["option_id"], "code": "day_has_no_date"})
            continue
        if asked >= MAX_REFRESH:
            failed.append({"option_id": plan["option_id"], "code": "refresh_limit"})
            continue
        asked += 1
        try:
            answers.append((plan, await quote(client, tenant_id, plan)))
        except (Conflict, Invalid) as exc:
            if exc.code == "aa_sold_out":
                answers.append((plan, AaQuote(None, None, "sold_out", dt.datetime.now(dt.UTC), None)))
            else:
                failed.append({"option_id": plan["option_id"], "code": exc.code})
    return answers, failed
