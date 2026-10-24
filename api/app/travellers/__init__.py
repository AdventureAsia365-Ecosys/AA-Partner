"""The agency's travellers: F1 identity vault and F2 profile (AA-937, design v3 card CRM-09, §4.3–4.4).

Modules (import each one directly; this package re-exports nothing, so importing one never pulls in another):

- :mod:`app.travellers.fields`: the F1 fields, the F2 codes, the reveal reasons and the input rules.
- :mod:`app.travellers.reveal`: ``reveal(field, reason)``, the **only** way agency code opens one F1 field of one
  traveller; one ``reveal`` audit event per field. The public routes never import it (``tests/unit/
  test_reveal_boundary.py``).
- :mod:`app.travellers.self_reveal`: ``traveller_self_reveal(session)``, what a traveller may see of their own record
  (the client portal, CRM-24a): masked passport, never another traveller.
- :mod:`app.travellers.age_bands`: the age band at departure (R32) and its recompute, only when a DOB or a trip start
  date changes.
- :mod:`app.travellers.service`: the client directory (one ``list_view`` per page), the profile (one ``view``), create
  and edit, the F2 profile, health codes, relationships (R7) and the party of a trip.
"""
