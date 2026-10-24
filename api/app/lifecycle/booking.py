"""The Booking re-check a trip restore needs (card E "restore re-checks Booking"; design §3.5, lane §3.3).

The B2B client (``booking.b2b.v1`` over the M2M pool) arrives with CRM-19 (AA-947) / CRM-B1 (AA-972). This module is
the seam it plugs into: :class:`BookingLineChecker` answers the current status of AA lines by their Booking reference
(hold or booking id). Until a checker is configured (``app.state.booking_checker``), restoring a trip that still has
an open AA line in the ledger answers 503 ``booking_not_configured`` and the trip stays in the trash; a trip without
AA lines restores without asking Booking.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.errors import Unavailable

# The aa_line codes of crm.trip_commitment (crm-010 CHECK).
LINE_STATUSES = frozenset({"requested", "held", "confirmed", "released", "expired", "cancelled", "declined"})


class BookingUnavailable(Unavailable):
    """Booking could not answer (not configured, down, or it does not know a line)."""


class BookingLineChecker(Protocol):
    async def line_statuses(self, tenant_id: uuid.UUID, refs: list[str]) -> dict[str, str]:
        """Booking's current status of each AA line reference (missing ref = unknown to Booking)."""
        ...
