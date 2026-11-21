"""The agency lines of an accepted proposal (AA-1147; S285 decision T18; crm-083).

When the client accepts a builder version, every agency item it chose (flight, visa, travel insurance, the agency fee:
:mod:`app.ledger.agency_items`) becomes one **planning** agency booking line of the trip, in the same transaction as
the accept, so the agent never types it again and the ledger holds what the client agreed to:

- the title, the item type, the supplier (none = "Handled by the agency") and the "folded into the tour price" flag of
  the item;
- the sell = the item's share of the accepted total (:func:`app.proposals.builder.line_sells`: after the version's
  discount), the cost = the item's net when the agent typed one;
- the service date = the date of the item's day (none for the "Before you go" day);
- ``option_id`` = the item, unique per agency: a second accept call writes nothing.

A block the client left "for later" (Fix B9: agency items only) is not decided yet, so it writes no line. AA items are
not touched here: the Booking sync writes their lines from the accepted hold (:mod:`app.booking.proposal_lines`), and
an agency line is never sent to Adventure Asia (the sync, the ops release and the AA invoices read ``supplier_kind =
'aa'`` lines only; ``tests/integration/test_aa1147_agency_extras_api.py`` proves it).

The client link has no member role, so the lines and their money are written by ``crm.agency_lines_from_proposal``
(SECURITY DEFINER, crm-083), which re-checks that the version is accepted, that each item is a live agency item of it
and that no sell is above the item's own.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.proposals import builder
from app.tenancy import AnyContext

WRITE_SQL = "SELECT crm.agency_lines_from_proposal($1, $2::jsonb)"


def chosen(s: dict[str, Any], choice: dict[str, str], later: set[str], starts_on: Any) -> list[dict[str, str | None]]:
    """The agency items of the client's choice (blocks left for later aside) as the writer's input: option id, sell
    (its share of the accepted total) and the service date."""
    options = builder._option_map(s)
    sells = builder.line_sells(s, choice)
    days = {str(d["proposal_day_id"]): d["day_no"] for d in s["days"]}
    blocks = {str(b["option_block_id"]): str(b["proposal_day_id"]) for b in s["blocks"]}
    out = []
    for block_id, option_id in sorted(choice.items()):
        o = options.get(option_id)
        if o is None or o["source"] != "agency" or block_id in later:
            continue
        day = builder._day_date(starts_on, int(days.get(blocks.get(block_id, ""), 0)))
        out.append({
            "option_id": option_id,
            "sell": str(sells.get(option_id, o["sell_amount"])),
            "service_from": day.isoformat() if day else None,
        })  # fmt: skip
    return out


async def write_accepted(
    ctx: AnyContext, proposal_id: uuid.UUID, s: dict[str, Any], choice: dict[str, str], later: set[str], starts_on: Any
) -> int:
    """Write the agency lines of the version just accepted (in the accept's transaction); returns how many."""
    items = chosen(s, choice, later, starts_on)
    if not items:
        return 0
    made = int(await ctx.conn.fetchval(WRITE_SQL, proposal_id, json.dumps(items)) or 0)
    if made:
        ctx.audit("agency_lines_from_proposal", "allowed", "proposal", str(proposal_id))
    return made
