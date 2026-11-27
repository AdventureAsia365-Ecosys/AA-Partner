"""The agency's booking conditions with AA in the CRM (AA-1188; booking.b2b.v1 ``GET /terms``, 1.3 + 1.12).

What AA's terms say before the agency quotes: its deposit % and when the balance is billed, the default cancellation
fee tiers, AA's approved clauses (for example how AA corrects a clearly wrong price) and the quote rules (how long a
quote holds its price, the group size AA quotes on request, the child and infant age bands). Read live from Booking
after the membership check (no copy is kept: the terms change rarely and the page is opened rarely), through
:func:`app.booking.contract.partner_terms` (a field that breaks the contract fails the whole answer: 503).

Errors (:func:`booking_error`): ``unknown_partner`` / ``forbidden`` -> 409 ``aa_partner_not_active`` (the same as the
AA prices page); any other refusal -> 422 ``aa_terms_refused`` with Booking's code; Booking down or a contract break ->
503 ``booking_unavailable`` with ``Retry-After``. No price and no net crosses here.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.booking.tariff import RETRY_AFTER_S
from app.errors import Conflict, Invalid, ServiceError, UnavailableRetry


def booking_error(exc: Exception) -> ServiceError:
    """The CRM Problem for a Booking failure of a terms read (codes only, never Booking's message)."""
    if isinstance(exc, BookingRefusal):
        if exc.code in ("unknown_partner", "forbidden"):
            return Conflict(
                "aa_partner_not_active", "Your agency is not linked to AA B2B, or AA has suspended the link"
            )
        return Invalid("aa_terms_refused", "AA refused the terms request", aa_code=exc.code)
    return UnavailableRetry(
        "booking_unavailable", RETRY_AFTER_S, "AA cannot be reached right now; try again in a minute"
    )


async def read(client: BookingClient, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The agency's terms with AA as the CRM's JSON. Raises the :func:`booking_error` Problem."""
    try:
        return contract.partner_terms(await client.terms(tenant_id))
    except (BookingRefusal, BookingUnavailable, contract.ContractError) as exc:
        raise booking_error(exc) from None
