"""Agency reports and AA insights (card CRM-34, AA-966; spec modules 16 and 17, design v3 A-14 / A-01 / S-03, R23, R28).

- :mod:`app.reports.period`: the report window in the agency's time zone and the comparison (same period last year,
  or the last 3 months for an agency without a year of history, R28 E).
- :mod:`app.reports.service`: the agency's reports, read-only SQL over ``crm.*`` inside ``tenant_tx()`` (RLS by
  agency): tiles, funnel, Lost / cancel reasons, lead sources, open trips, option picks, money (owner / agent only),
  outstanding payments, team activity and the spec §6 success metrics.
- :mod:`app.reports.export`: one section as CSV (spreadsheet formula guard; money sections need ``view_margin``).
- :mod:`app.reports.insights`: the cross-agency aggregate of ``crm.v_insight_fact`` (module 17): a cell is published
  only with at least 10 travellers from at least 3 agencies; an opted-out agency never contributes.
"""
