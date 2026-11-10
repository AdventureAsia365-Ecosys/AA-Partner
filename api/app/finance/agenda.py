"""The Home "today" tiles (AA-1068 item crm-31, the dashboard part): what needs the agency's attention now.

- ``open_proposals``: proposals published or sent to the client, not accepted yet, on live trips.
- ``deposits_due_week``: deposit schedule rows still due in the next 7 days (today included, agency time zone).
- ``overdue_instalments``: any schedule row (deposit, balance, a later instalment kind) due before today, not paid.
- ``passport_missing``: booked / pre-departure trips leaving in the next :data:`PASSPORT_DAYS` days with a traveller
  whose passport number or expiry is not on file (presence only: the values stay encrypted, nothing is decrypted
  here). Visa checks are a later card.
- ``next_departures``: the group departures (AA departure id or the agency's group code) of trips leaving in the
  next :data:`DEPARTURE_DAYS` days, with trips and travellers. ``guaranteed`` is null until the departure roster card
  stores AA's departure status from the Booking feed (it ignores ``departure.guaranteed`` today).
- ``aa_overdue_invoices`` (owner / agent only, else null): the AA invoices of the "Bookings with AA" copy that are past
  due with a balance.

Counts and ids only (no amount), so every role reads them; the owner filter is not applied (the agency's day).
"""

from __future__ import annotations

from typing import Any

from app.margin import sees_margin
from app.tenancy import TenantContext

PASSPORT_DAYS = 60
DEPARTURE_DAYS = 30
MAX_DEPARTURES = 5
DEFAULT_TZ = "Asia/Ho_Chi_Minh"

COUNTS_SQL = """
WITH today AS (
    SELECT (now() AT TIME ZONE COALESCE((SELECT timezone FROM crm.tenant_settings), $1::text))::date AS d
)
SELECT
    (SELECT count(*) FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
      WHERE p.deleted_at IS NULL AND p.status IN ('published', 'sent')
        AND t.stage NOT IN ('lost', 'cancelled', 'completed'))::int AS open_proposals,
    (SELECT count(*) FROM crm.payment_schedule s JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
      WHERE s.deleted_at IS NULL AND s.status = 'due' AND s.kind = 'deposit'
        AND s.due_on BETWEEN (SELECT d FROM today) AND (SELECT d FROM today) + 6)::int AS deposits_due_week,
    (SELECT count(*) FROM crm.payment_schedule s JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
      WHERE s.deleted_at IS NULL AND s.status = 'due' AND s.due_on < (SELECT d FROM today)
        AND t.stage NOT IN ('lost', 'cancelled'))::int AS overdue_instalments,
    (SELECT count(DISTINCT t.trip_id) FROM crm.trip t
       JOIN crm.trip_traveler tt ON tt.trip_id = t.trip_id AND tt.deleted_at IS NULL
       JOIN crm.traveler tr ON tr.traveler_id = tt.traveler_id AND tr.deleted_at IS NULL
      WHERE t.deleted_at IS NULL AND t.stage IN ('booked', 'pre_departure')
        AND t.start_date BETWEEN (SELECT d FROM today) AND (SELECT d FROM today) + $2::int
        AND (tr.passport_no_enc IS NULL OR tr.passport_expiry_enc IS NULL))::int AS passport_missing
"""
DEPARTURES_SQL = """
WITH today AS (
    SELECT (now() AT TIME ZONE COALESCE((SELECT timezone FROM crm.tenant_settings), $1::text))::date AS d
)
SELECT d.departure_ref, min(d.start_date) AS start_date, count(*)::int AS trips, sum(d.pax)::int AS pax
  FROM crm.v_departure_trip d
 WHERE d.stage IN ('booked', 'pre_departure', 'travelling')
   AND d.start_date BETWEEN (SELECT d FROM today) AND (SELECT d FROM today) + $2::int
 GROUP BY d.departure_ref
 ORDER BY min(d.start_date), d.departure_ref
 LIMIT $3
"""
AA_OVERDUE_SQL = """
SELECT count(*)::int FROM crm.aa_invoice_cache
 WHERE deleted_at IS NULL AND aa_status = 'issued' AND overdue_days > 0 AND balance_amount > 0
"""


async def agenda(ctx: TenantContext) -> dict[str, Any]:
    counts = await ctx.conn.fetchrow(COUNTS_SQL, DEFAULT_TZ, PASSPORT_DAYS)
    departures = await ctx.conn.fetch(DEPARTURES_SQL, DEFAULT_TZ, DEPARTURE_DAYS, MAX_DEPARTURES)
    aa_overdue = await ctx.conn.fetchval(AA_OVERDUE_SQL) if sees_margin(ctx.member_role) else None
    return {
        "open_proposals": counts["open_proposals"],
        "deposits_due_week": counts["deposits_due_week"],
        "overdue_instalments": counts["overdue_instalments"],
        "passport_missing": counts["passport_missing"],
        "passport_days": PASSPORT_DAYS,
        "aa_overdue_invoices": aa_overdue,
        "next_departures": [
            {
                "departure_ref": d["departure_ref"],
                "start_date": d["start_date"].isoformat() if d["start_date"] else None,
                "trips": d["trips"],
                "pax": d["pax"],
                "guaranteed": None,
            }
            for d in departures
        ],
        "departure_days": DEPARTURE_DAYS,
    }
