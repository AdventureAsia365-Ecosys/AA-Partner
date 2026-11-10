"""AA-1068: CRM finance (S275 study items crm-25, crm-26, crm-30, crm-31, crm-32; migration crm-051).

- :mod:`app.finance.pnl`: the profit of one trip (``crm.v_trip_pnl``) and the profit cube of the Reports page.
- :mod:`app.finance.bills`: the bills of the agency's own suppliers (``crm.supplier_bill`` + lines) and the actual cost
  of each billed line (money kind ``actual_cost`` at the bill FX, ``crm.booking_line_actual_cost``).
- :mod:`app.finance.aa_invoices`: the "Bookings with AA" page: the agency's AA invoices and billing status read live
  from Booking (booking.b2b.v1 1.5), kept in ``crm.aa_invoice_cache``, linked to the trips' AA lines.
- :mod:`app.finance.departures`: the costs of a group departure (``crm.departure_cost``), split by pax into each trip,
  the departure's own P&L, and the trip's group code.
- :mod:`app.finance.agenda`: the Home "today" tiles (open proposals, deposits due this week, overdue instalments,
  missing passport details, next departures): counts only, every role.

Every money read and write is owner / agent only (``view_margin`` / ``money_fields_edit``): the routes refuse the
assistant before any read, and the database's RESTRICTIVE ``*_margin_roles`` policies return nothing to it anyway.
"""
