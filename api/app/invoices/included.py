"""The "included" rows of an invoice: what the charge covers (AA-952), read from the trip's live booking lines.

AA-1147 (S285 decision T18, crm-083): an agency line (flight, visa, travel insurance, the agency fee) carries the
client's price of that service in the invoice's currency, printed under "Services provided by <agency>" and never
summed (totals and the issue check read charge rows only); a flight or fee folded into the tour price (``bundled``),
or a line without a sell in that currency, carries none. AA lines stay titles only (design §2.9 rule 3).
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from app.tenancy import AnyContext

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
INCLUDED_SQL = """
SELECT l.title, l.supplier_kind, l.bundled,
       (SELECT m.amount FROM crm.v_booking_line_money_current m
         WHERE m.booking_line_id = l.booking_line_id AND m.kind = 'sell' AND m.currency = $3) AS sell
  FROM crm.booking_line l
 WHERE l.trip_id = $1 AND l.deleted_at IS NULL AND l.status <> 'cancelled'
 ORDER BY l.service_from NULLS LAST, l.created_at, l.booking_line_id
 LIMIT $2
"""


async def rows(
    ctx: AnyContext, trip_id: uuid.UUID, limit: int, currency: str, max_description: int
) -> list[tuple[str, str, Decimal | None]]:
    """``(source, description, price)`` of each included row, in service order (blank titles left out)."""
    out = []
    for r in await ctx.conn.fetch(INCLUDED_SQL, trip_id, limit, currency.strip()):
        title = CONTROL.sub(" ", r["title"] or "").strip()[:max_description]
        if not title:
            continue
        source = "aa_line" if r["supplier_kind"] == "aa" else "agency_line"
        price = r.get("sell") if source == "agency_line" and not r.get("bundled") else None
        out.append((source, title, price))
    return out
