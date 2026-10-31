"""Module 8 AA ops release (AA-956, card CRM-26; design v3 M8, F8.3 "Ops release", Fix B2, Fix B3, S-02, R33, R44).

When an AA line of a trip is confirmed, AA ground operations need the travellers' details to run the departure. The
agency stays the controller: the details never leave the CRM (DM-6.8). They are copied into one ``crm.ops_release``
row per trip, under the agency's ``ops_release`` DEK, read by AA ops (WORKFORCE group ``crm-ops``) inside AA-Partner,
and every view writes "AA ops viewed trip X" into the agency's audit.

- :mod:`app.ops_release.build` — the release document (minimal fields, Q1 3.10) and its completeness.
- :mod:`app.ops_release.service` — create / refresh / T-21 alert / withdraw / purge in one agency, and the agency's
  own view of its trip's release (the trip workspace panel, A-03 Activity).
- :mod:`app.ops_release.job` — ``crm_ops_release_sync``: hourly for every agency (due-now hook), and at once for one
  trip when Booking confirms or cancels an AA line (a nudge queued by ``crm_booking_sync``) or a member withdraws.
- :mod:`app.ops_release.staff` — S-02 for AA ops: the board (no personal data), one release (audited), the minimal
  supplier manifest (audited, watermarked), the link to the Booking incident admin (Fix B3: the flag is set there).
"""
