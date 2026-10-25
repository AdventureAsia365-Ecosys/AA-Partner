"""The Booking re-check of a trip restore (AA-942 seam :class:`app.lifecycle.booking.BookingLineChecker`; AA-947).

A trip in the trash that still has an open AA commitment (``crm.trip_commitment`` kind ``aa_line``, ``source_ref`` =
the Booking hold id) is restored only after Booking says where each hold stands now: ``GET /holds/{id}`` per hold,
``active`` -> ``held``, ``confirmed``, ``expired``, ``released``. A hold Booking does not know (404) is left out (the
caller answers 503 ``booking_line_unknown``); Booking down is 503 ``booking_unavailable``: the trip stays in the trash.
"""

from __future__ import annotations

import uuid

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.lifecycle import booking as seam

HOLD_TO_LINE = {"active": "held", "confirmed": "confirmed", "expired": "expired", "released": "released"}


class BookingChecker:
    def __init__(self, client: BookingClient) -> None:
        self._client = client

    async def line_statuses(self, tenant_id: uuid.UUID, refs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for ref in refs:
            try:
                data = await self._client.get_hold(tenant_id, ref)
                out[ref] = HOLD_TO_LINE[contract.hold(data).status]
            except BookingRefusal as exc:
                if exc.code != "not_found":
                    raise seam.BookingUnavailable("booking_unavailable") from None
            except (BookingUnavailable, contract.ContractError, ValueError):
                raise seam.BookingUnavailable("booking_unavailable") from None
        return out
