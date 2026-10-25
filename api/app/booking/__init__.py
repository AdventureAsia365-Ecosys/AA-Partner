"""CRM-19 (AA-947): the B2B client of AA-Booking and the Adventure Asia lines of a trip (design v3 M3 / M6 AA, §5.2).

- :mod:`app.booking.client`: ``booking.b2b.v1`` over the aa-m2m pool (one scope per call), transient vs. final failures.
- :mod:`app.booking.contract`: a tolerant reader of the 1.1 answers.
- :mod:`app.booking.views`: the ``aa`` block of a line and the AA-aware Lost hint (pure functions).
- :mod:`app.booking.aa_lines`: what a member may ask for (an AA line from a quote, hold, confirm, release, re-quote);
  every ask is a row of the outage queue ``crm.aa_request``; the line itself stays read-only for members.
- :mod:`app.booking.sync`: ``crm_booking_sync`` (every minute and on a nudge): sends the due requests, applies what
  Booking answered, and pulls the agency's ``/events`` feed into ``crm.inbox_events``.
- :mod:`app.booking.checker`: the restore re-check the AA-942 seam asks for (``app.state.booking_checker``).
- :mod:`app.booking.products`: the live product / departure / "from"-price lookup of the add form, cached briefly.

:func:`refusal` maps the crm-017 SQLSTATEs (CRM33, CRM34) next to the crm-014 ones (:func:`app.ledger.refusal`).
"""

from __future__ import annotations

import asyncpg

from app.errors import Conflict, Forbidden, Invalid, ServiceError
from app.ledger import refusal as ledger_refusal


def _detail(exc: asyncpg.PostgresError, code: str) -> str:
    text = str(exc)
    marker = f"{code}:"
    return text.split(marker, 1)[1].split(" ", 1)[0].strip() if marker in text else ""


def refusal(exc: asyncpg.PostgresError) -> ServiceError | None:
    """The service error for a crm-017 (or crm-014) refusal, or None (the caller re-raises)."""
    state = getattr(exc, "sqlstate", "") or ""
    if state == "CRM33":
        reason = _detail(exc, "aa_request_invalid")
        if reason == "trip_closed":
            return Conflict("trip_closed", "a lost, cancelled or completed trip takes no AA line")
        return Invalid("aa_request_invalid", "check the AA item's date, travellers and rooms", reason=reason or None)
    if state == "CRM34":
        return Conflict("aa_tour_not_served", "this tour is not in the AA catalog any more")
    if isinstance(exc, asyncpg.InsufficientPrivilegeError) and "margin_forbidden" in str(exc):
        return Forbidden("role_forbidden", "only an owner or an agent sets a sell amount")
    return ledger_refusal(exc)
