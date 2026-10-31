# ruff: noqa: S608 — every f-string interpolation below is a constant of this module, never a caller value.
"""The read-only SQL of the agency reports (AA-966). Every statement runs on the connection of a ``tenant_tx()``
context, so ``crm.*`` FORCE RLS keeps it inside the caller's agency; trashed rows (``deleted_at``) never count.

Windowed statements share the parameters ``$1`` start date, ``$2`` end date + 1 (both local dates), ``$3`` the agency
time zone and ``$4`` the owner filter (a member id, or NULL for everyone). :func:`in_window` writes the bound check.
"""

from __future__ import annotations

from app.invoices.service import RECEIVABLE

# Forward stages in order (crm-005 trip_stage_chk). Lost and cancelled are outcomes, not steps.
FORWARD_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling", "completed")
OPEN_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling")
BOOKED_RANK = FORWARD_STAGES.index("booked") + 1  # array_position is 1-based
STALLED_DAYS = 14  # report catalog CRM-01 "Stalled trips"
QUIET_DAYS = 3  # spec module 2: "no lead goes quiet for more than 3 days unnoticed"


def in_window(column: str) -> str:
    return f"{column} >= ($1::date::timestamp AT TIME ZONE $3) AND {column} < ($2::date::timestamp AT TIME ZONE $3)"


OWNER = "($4::uuid IS NULL OR t.owner_member_id = $4)"
# A history row that a later Undo reversed does not count (crm-012: the undo row names it in undo_of).
_NOT_UNDONE = "NOT EXISTS (SELECT 1 FROM crm.trip_stage_history u WHERE u.undo_of = h.history_id)"

SETTINGS_SQL = """
SELECT (now() AT TIME ZONE coalesce(s.timezone, $1))::date AS today,
       coalesce(s.timezone, $1) AS tz,
       coalesce(s.base_currency, 'USD') AS base_currency,
       (SELECT min(t.created_at AT TIME ZONE coalesce(s.timezone, $1))::date
          FROM crm.trip t WHERE t.deleted_at IS NULL) AS first_day
  FROM (SELECT 1) one
  LEFT JOIN crm.tenant_settings s ON s.deleted_at IS NULL
"""

# The cohort: trips created in the window. max_rank = the furthest forward stage the trip reached (its current stage,
# or a history row not undone), so a trip lost after a proposal counts in inquiry, qualifying and proposal.
COHORT_SQL = f"""
WITH cohort AS (
    SELECT t.trip_id, t.stage, t.owner_member_id, coalesce(t.lead_source, '') AS lead_source,
           (t.budget_band IS NOT NULL AND t.budget_band <> 'not_sure') AS has_budget
      FROM crm.trip t
     WHERE t.deleted_at IS NULL AND {in_window("t.created_at")} AND {OWNER}
)
SELECT c.stage, c.owner_member_id, c.lead_source, c.has_budget,
       greatest(coalesce(array_position($5::text[], c.stage), 0),
                coalesce((SELECT max(array_position($5::text[], h.to_stage)) FROM crm.trip_stage_history h
                           WHERE h.trip_id = c.trip_id AND {_NOT_UNDONE}), 0)) AS max_rank,
       count(*)::int AS n
  FROM cohort c
 GROUP BY 1, 2, 3, 4, 5
"""

# Trips that ended lost or cancelled in the window (stage_entered_at = when they ended), by reason (R23).
OUTCOME_SQL = f"""
SELECT t.stage, coalesce(t.lost_reason_code, t.cancel_reason_code) AS reason, count(*)::int AS n
  FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage IN ('lost', 'cancelled') AND {in_window("t.stage_entered_at")} AND {OWNER}
 GROUP BY 1, 2
"""

# Cycle time: trips whose first (not undone) move to booked is in the window, days from creation.
CYCLE_SQL = f"""
SELECT count(*)::int AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM b.at - t.created_at) / 86400.0) AS median_days
  FROM crm.trip t
  JOIN LATERAL (SELECT min(h.at) AS at FROM crm.trip_stage_history h
                 WHERE h.trip_id = t.trip_id AND h.to_stage = 'booked' AND {_NOT_UNDONE}) b ON b.at IS NOT NULL
 WHERE t.deleted_at IS NULL AND {in_window("b.at")} AND {OWNER}
"""

# Open trips now, by stage (live, not windowed): stalled = in the stage for more than 14 days.
OPEN_SQL = f"""
SELECT t.stage, count(*)::int AS n,
       count(*) FILTER (WHERE t.stage_entered_at < now() - interval '{STALLED_DAYS} days')::int AS stalled,
       count(*) FILTER (WHERE t.stage_entered_at < now() - interval '{STALLED_DAYS} days'
                          AND t.waiting_on = 'none')::int AS stalled_not_waiting,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM now() - t.stage_entered_at) / 86400.0)
           AS median_age_days
  FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage = ANY($1::text[]) AND ($2::uuid IS NULL OR t.owner_member_id = $2)
 GROUP BY 1
"""

# The client's current picks changed in the window (R19 / module 3): kept the suggested option, or an upgrade
# (an option dearer than the block's suggested one).
PICKS_SQL = f"""
SELECT ob.type AS block_type,
       coalesce(s.option_id = ob.default_option_id, false) AS kept_default,
       coalesce(o.sell_amount > d.sell_amount, false) AS upgraded,
       count(*)::int AS n
  FROM crm.selection s
  JOIN crm.option_block ob ON ob.option_block_id = s.option_block_id
  JOIN crm.option o ON o.option_id = s.option_id
  LEFT JOIN crm.option d ON d.option_id = ob.default_option_id
  JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
 WHERE s.deleted_at IS NULL AND s.chosen_by = 'client' AND NOT s.choose_later
   AND {in_window("s.updated_at")} AND {OWNER}
 GROUP BY 1, 2, 3
"""

# Confirmed booking lines by confirmation date (R18: only confirmed lines count), with the head money row per kind in
# the agency's base currency ($5). Owner / agent only: crm-014's RESTRICTIVE policy returns no money to anyone else.
MONEY_SQL = f"""
SELECT bl.booking_line_id, bl.trip_id, bl.supplier_kind, bl.title,
       date_trunc('month', bl.confirmed_at AT TIME ZONE $3)::date AS month,
       coalesce(t.party_type, '') AS party_type,
       max(m.amount_base) FILTER (WHERE m.kind = 'sell') AS sell,
       max(m.amount_base) FILTER (WHERE m.kind = 'cost') AS cost,
       max(m.amount_base) FILTER (WHERE m.kind = 'commission_expected') AS commission
  FROM crm.booking_line bl
  JOIN crm.trip t ON t.trip_id = bl.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.v_booking_line_money_current m
         ON m.booking_line_id = bl.booking_line_id AND m.base_currency = $5
 WHERE bl.deleted_at IS NULL AND bl.status = 'confirmed' AND {in_window("bl.confirmed_at")} AND {OWNER}
 GROUP BY bl.booking_line_id, bl.trip_id, bl.supplier_kind, bl.title, 5, 6
"""

# Confirmed lines by kind only (no money): the AA share for a role that cannot read money.
LINE_COUNT_SQL = f"""
SELECT bl.supplier_kind, count(*)::int AS n
  FROM crm.booking_line bl
  JOIN crm.trip t ON t.trip_id = bl.trip_id AND t.deleted_at IS NULL
 WHERE bl.deleted_at IS NULL AND bl.status = 'confirmed' AND {in_window("bl.confirmed_at")} AND {OWNER}
 GROUP BY 1
"""

# Outstanding money, now ($1 = today, local; $2 = owner filter). Amounts in each document's own currency.
# Open invoices follow A-10 Receivables (app.invoices.service.receivables): the RECEIVABLE statuses, paid = payments
# only (a refund never reopens an invoice), and each invoice's balance clamped at 0 BEFORE the currency sum, so an
# overpaid invoice never lowers another one's balance. The Reports and Payments pages show the same amount.
_RECEIVABLE = ", ".join(f"'{s}'" for s in RECEIVABLE)
OPEN_INVOICES_SQL = f"""
SELECT i.currency, count(*)::int AS n, sum(i.total_amount) AS total,
       sum(coalesce(p.paid, 0)) AS paid,
       sum(greatest(i.total_amount - coalesce(p.paid, 0), 0)) AS outstanding,
       count(*) FILTER (WHERE i.due_on < $1)::int AS overdue_n,
       coalesce(sum(greatest(i.total_amount - coalesce(p.paid, 0), 0)) FILTER (WHERE i.due_on < $1), 0) AS overdue
  FROM crm.invoice i
  JOIN crm.trip t ON t.trip_id = i.trip_id AND t.deleted_at IS NULL
  LEFT JOIN LATERAL (SELECT sum(p.amount) AS paid
                       FROM crm.payment p WHERE p.invoice_id = i.invoice_id AND p.entry_kind = 'payment') p ON true
 WHERE i.deleted_at IS NULL AND i.status IN ({_RECEIVABLE})
   AND ($2::uuid IS NULL OR t.owner_member_id = $2)
 GROUP BY 1
"""
# Deposit / balance schedule rows NOT INVOICED YET: a row billed by a live invoice (anything but a draft or a void one)
# stays 'due' until that invoice is paid in full, and its money is already in OPEN_INVOICES_SQL, so it is left out here
# (never counted twice).
SCHEDULE_DUE_SQL = """
SELECT s.currency, s.kind, count(*)::int AS n, sum(s.amount) AS total,
       count(*) FILTER (WHERE s.due_on < $1)::int AS overdue_n,
       coalesce(sum(s.amount) FILTER (WHERE s.due_on < $1), 0) AS overdue
  FROM crm.payment_schedule s
  JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
 WHERE s.deleted_at IS NULL AND s.status = 'due' AND ($2::uuid IS NULL OR t.owner_member_id = $2)
   AND NOT EXISTS (SELECT 1 FROM crm.invoice i
                    WHERE i.schedule_id = s.schedule_id AND i.deleted_at IS NULL
                      AND i.status NOT IN ('draft', 'void'))
 GROUP BY 1, 2
"""
# Received in the window ($1 start, $2 end + 1 as dates: received_on is a date already).
COLLECTED_SQL = """
SELECT p.currency,
       coalesce(sum(p.amount) FILTER (WHERE p.entry_kind = 'payment'), 0) AS received,
       coalesce(sum(p.amount) FILTER (WHERE p.entry_kind = 'refund'), 0) AS refunded,
       count(*) FILTER (WHERE p.entry_kind = 'payment')::int AS n
  FROM crm.payment p
  JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.received_on >= $1 AND p.received_on < $2 AND ($3::uuid IS NULL OR t.owner_member_id = $3)
 GROUP BY 1
"""

# Team activity per member (ids and roles only; names live in the identity provider). The owner filter ($4) keeps
# that one member's row.
TEAM_SQL = f"""
SELECT m.member_id, m.role, m.status,
       (SELECT count(*) FROM crm.trip t WHERE t.owner_member_id = m.member_id AND t.deleted_at IS NULL
           AND t.stage = ANY($5::text[]))::int AS open_trips,
       (SELECT count(*) FROM crm.task k WHERE k.assignee_member_id = m.member_id AND k.deleted_at IS NULL
           AND k.status = 'open' AND k.due_at < now())::int AS overdue_tasks,
       (SELECT count(*) FROM crm.task k WHERE k.assignee_member_id = m.member_id AND k.deleted_at IS NULL
           AND k.status = 'done' AND {in_window("k.done_at")})::int AS tasks_done,
       (SELECT count(*) FROM crm.message g WHERE g.author_member_id = m.member_id AND g.deleted_at IS NULL
           AND g.direction = 'out' AND g.status = 'sent' AND {in_window("g.sent_at")})::int AS messages_sent
  FROM crm.member m
 WHERE m.deleted_at IS NULL AND m.status <> 'invited' AND ($4::uuid IS NULL OR m.member_id = $4)
 ORDER BY m.role, m.created_at, m.member_id
"""
UNASSIGNED_SQL = """
SELECT count(*)::int FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.owner_member_id IS NULL AND t.stage = ANY($1::text[])
"""
# First reply per trip created in the window: minutes from the inquiry (trip creation) to the first message the
# agency sent from the CRM on that trip (imported history and drafts do not count). NULL = no reply yet.
FIRST_REPLY_SQL = f"""
SELECT t.owner_member_id, extract(epoch FROM fo.at - t.created_at) / 60.0 AS minutes
  FROM crm.trip t
  LEFT JOIN LATERAL (
      SELECT min(g.sent_at) AS at
        FROM crm.message g JOIN crm.conversation c ON c.conversation_id = g.conversation_id
       WHERE c.trip_id = t.trip_id AND g.direction = 'out' AND g.status = 'sent' AND NOT g.imported
         AND g.deleted_at IS NULL AND g.sent_at >= t.created_at) fo ON true
 WHERE t.deleted_at IS NULL AND {in_window("t.created_at")} AND {OWNER}
"""
# Conversations whose latest message is from the client and older than 24 h (report catalog CRM-02).
UNANSWERED_SQL = """
SELECT count(*)::int
  FROM crm.conversation c
  JOIN LATERAL (SELECT g.direction, g.occurred_at FROM crm.message g
                 WHERE g.conversation_id = c.conversation_id AND g.deleted_at IS NULL
                   AND g.status IN ('received', 'sent')
                 ORDER BY g.occurred_at DESC LIMIT 1) last ON true
  LEFT JOIN crm.trip t ON t.trip_id = c.trip_id
 WHERE c.deleted_at IS NULL AND last.direction = 'in' AND last.occurred_at < now() - interval '24 hours'
   AND ($1::uuid IS NULL OR t.owner_member_id = $1)
"""

# ── Spec §6 success metrics: one source query each (module number in the name) ───────────────────────────────
# Module 2: open trips quiet for 3 days with no open task on them.
QUIET_SQL = f"""
SELECT count(*)::int FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage = ANY($1::text[]) AND t.last_activity_at < now() - interval '{QUIET_DAYS} days'
   AND NOT EXISTS (SELECT 1 FROM crm.task k WHERE k.trip_id = t.trip_id AND k.status = 'open' AND k.deleted_at IS NULL)
   AND ($2::uuid IS NULL OR t.owner_member_id = $2)
"""
TOP_PRODUCTS_SQL = f"""
SELECT bl.supplier_kind, bl.title, count(*)::int AS lines, count(DISTINCT bl.trip_id)::int AS trips
  FROM crm.booking_line bl
  JOIN crm.trip t ON t.trip_id = bl.trip_id AND t.deleted_at IS NULL
 WHERE bl.deleted_at IS NULL AND bl.status = 'confirmed' AND {in_window("bl.confirmed_at")} AND {OWNER}
 GROUP BY 1, 2
 ORDER BY 3 DESC, 4 DESC, 2
 LIMIT $5
"""
INSIGHTS_INCLUDED_SQL = """
SELECT NOT coalesce((SELECT e.insights_opt_out FROM crm.tenant_entitlement e WHERE e.deleted_at IS NULL), false)
"""

# Module 3: messages per trip that first reached booked in the window (median; the spec's "about 8 to 2-3").
MESSAGES_PER_BOOKED_SQL = f"""
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(x.n, 0)) AS median, count(*)::int AS trips
  FROM crm.trip t
  JOIN LATERAL (SELECT min(h.at) AS at FROM crm.trip_stage_history h
                 WHERE h.trip_id = t.trip_id AND h.to_stage = 'booked' AND {_NOT_UNDONE}) b ON b.at IS NOT NULL
  LEFT JOIN LATERAL (SELECT count(*) AS n FROM crm.message g JOIN crm.conversation c
                       ON c.conversation_id = g.conversation_id
                      WHERE c.trip_id = t.trip_id AND g.deleted_at IS NULL AND NOT g.imported
                        AND g.status IN ('received', 'sent')) x ON true
 WHERE t.deleted_at IS NULL AND {in_window("b.at")} AND {OWNER}
"""
# Module 4: minutes from a text proposal's creation to its sending, for text proposals sent in the window.
TEXT_PROPOSAL_SQL = f"""
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM p.sent_at - p.created_at) / 60.0) AS median,
       count(*)::int AS n
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.deleted_at IS NULL AND p.kind = 'text' AND p.sent_at IS NOT NULL AND {in_window("p.sent_at")} AND {OWNER}
"""
# Module 5 / 9: messages sent from the CRM in the window, by channel.
SENT_BY_CHANNEL_SQL = f"""
SELECT g.channel, count(*)::int AS n
  FROM crm.message g
  JOIN crm.conversation c ON c.conversation_id = g.conversation_id
  LEFT JOIN crm.trip t ON t.trip_id = c.trip_id
 WHERE g.deleted_at IS NULL AND g.direction = 'out' AND g.status = 'sent' AND {in_window("g.sent_at")}
   AND ($4::uuid IS NULL OR t.owner_member_id = $4)
 GROUP BY 1
"""
# Module 7: deposits paid within 48 hours of the client's acceptance, for proposals accepted in the window.
DEPOSIT_48H_SQL = f"""
SELECT count(*)::int AS accepted,
       count(*) FILTER (WHERE s.paid_at IS NOT NULL AND s.paid_at <= p.accepted_at + interval '48 hours')::int AS fast
  FROM crm.proposal p
  JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.payment_schedule s ON s.proposal_id = p.proposal_id AND s.kind = 'deposit' AND s.deleted_at IS NULL
 WHERE p.deleted_at IS NULL AND p.accepted_at IS NOT NULL AND {in_window("p.accepted_at")} AND {OWNER}
"""
# Module 10: open tasks past due on trips about to leave or under way.
OVERDUE_BEFORE_DEPARTURE_SQL = """
SELECT count(*)::int FROM crm.task k JOIN crm.trip t ON t.trip_id = k.trip_id AND t.deleted_at IS NULL
 WHERE k.deleted_at IS NULL AND k.status = 'open' AND k.due_at < now()
   AND t.stage IN ('booked', 'pre_departure', 'travelling') AND ($1::uuid IS NULL OR t.owner_member_id = $1)
"""
# Module 13 (AA-957): commissions never claimed from the supplier and already past their due date (agency timezone).
COMMISSION_UNCLAIMED_SQL = """
SELECT count(*)::int FROM crm.commission c JOIN crm.trip t ON t.trip_id = c.trip_id AND t.deleted_at IS NULL
 WHERE c.status = 'expected'
   AND c.due_on < (now() AT TIME ZONE COALESCE((SELECT timezone FROM crm.tenant_settings), 'Asia/Ho_Chi_Minh'))::date
   AND ($1::uuid IS NULL OR t.owner_member_id = $1)
"""
# Module 11: of the trips with a client portal link created in the window, how many clients opened it.
PORTAL_SQL = f"""
SELECT count(DISTINCT l.trip_id)::int AS trips,
       count(DISTINCT l.trip_id) FILTER (WHERE l.first_opened_at IS NOT NULL)::int AS opened
  FROM crm.client_link l JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.deleted_at IS NULL AND l.purpose = 'portal' AND {in_window("l.created_at")} AND {OWNER}
"""
# Module 12: trips that left in the window (start date) after reaching booked, and how many had every signature
# request signed before the start date (and at least one).
SIGNED_SQL = f"""
SELECT count(*)::int AS departed,
       count(*) FILTER (WHERE x.total > 0 AND x.signed = x.total)::int AS signed
  FROM crm.trip t
  JOIN LATERAL (SELECT count(*) FILTER (WHERE r.status <> 'void') AS total,
                       count(*) FILTER (WHERE r.status = 'signed'
                                          AND (r.signed_at AT TIME ZONE $3)::date < t.start_date) AS signed
                  FROM crm.signature_request r
                 WHERE r.trip_id = t.trip_id AND r.deleted_at IS NULL) x ON true
 WHERE t.deleted_at IS NULL AND t.start_date >= $1 AND t.start_date < $2
   AND t.stage IN ('booked', 'pre_departure', 'travelling', 'completed') AND {OWNER}
"""
# Module 8 (AA-956): ops release errors = AA departures in the window whose ops release was still incomplete (or
# missing while an AA line was confirmed) on the departure date. Counted from the release row as last refreshed; a
# withdrawn release (a cancel) is not an error.
OPS_RELEASE_ERRORS_SQL = f"""
SELECT count(*)::int FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.start_date >= $1 AND t.start_date < $2
   AND t.start_date <= (now() AT TIME ZONE $3)::date
   AND t.stage IN ('pre_departure', 'travelling', 'completed') AND {OWNER}
   AND EXISTS (SELECT 1 FROM crm.booking_line bl WHERE bl.trip_id = t.trip_id AND bl.supplier_kind = 'aa'
                  AND bl.status = 'confirmed' AND bl.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM crm.ops_release r WHERE r.trip_id = t.trip_id AND r.complete
                      AND r.withdrawn_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM crm.ops_release r WHERE r.trip_id = t.trip_id AND r.withdrawn_at IS NOT NULL)
"""
# Module 15: trips with confirmed lines in the window that mix AA and the agency's own suppliers.
MIXED_SQL = f"""
SELECT count(*)::int AS trips,
       count(*) FILTER (WHERE x.kinds = 2)::int AS mixed
  FROM (SELECT bl.trip_id, count(DISTINCT bl.supplier_kind) AS kinds
          FROM crm.booking_line bl JOIN crm.trip t ON t.trip_id = bl.trip_id AND t.deleted_at IS NULL
         WHERE bl.deleted_at IS NULL AND bl.status = 'confirmed' AND {in_window("bl.confirmed_at")} AND {OWNER}
         GROUP BY bl.trip_id) x
"""
# Module 16: report downloads in the window (each export writes a report_export audit event).
REPORT_EXPORTS_SQL = f"""
SELECT count(*)::int FROM crm.audit_event e
 WHERE e.action = 'report_export' AND e.outcome = 'allowed' AND {in_window("e.at")}
"""
# Module 18: portability exports that became ready in the window.
DATA_EXPORTS_SQL = f"""
SELECT count(*)::int FROM crm.export_job x
 WHERE x.deleted_at IS NULL AND x.completed_at IS NOT NULL AND {in_window("x.completed_at")}
"""
