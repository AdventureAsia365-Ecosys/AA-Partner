"""AA support access (AA-971, card CRM-13b; design v3 §4.8 L2 / L3, §2.5 S-04, WF-12 "AA support access").

- :mod:`app.support.grants`: the agency side (``/crm/v1/support-access``): every member sees whether AA support or
  break-glass is in the agency (the banner); the owner grants a window of 1 to 7 days (show identity or not) and
  revokes it ("End now"), which ends the support session at once.
- :mod:`app.support.sessions`: the staff side (``/crm/staff/v1``): a ``crm-support`` member opens a support session
  inside the owner's active grant (ticket required, one staff member at a time); a ``crm-ops`` member opens break-glass
  (reason code + ticket, at most 1 hour; the owners are mailed, AA ops alerted by the ``crm_breakglass_opened`` log
  event); either ends their own session.
- :mod:`app.support.reads`: what a live session may read (:func:`app.tenancy.support_tx`, role
  ``crm_support_reader``): the agency overview, the trip list, one trip with its stage history and, only when the
  session shows identity, the display names of its party. Every read is an audit event of that agency (actor kinds
  ``support`` / ``breakglass``: the owner's access log filters "AA support" / "AA emergency"). Nothing writes agency
  data: the reader role has no write privilege at all (crm-033).
"""
