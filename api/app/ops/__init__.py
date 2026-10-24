"""The L1 platform console ``crm.ops.v1`` (AA-941, card CRM-13a; design v3 S-01, the CIS admin "Agencies (CRM)").

- :mod:`app.ops.serializer`: the response allow-list every ``/crm/ops/v1`` route goes through (card R test);
- :mod:`app.ops.console`: the agency operations, each in :func:`app.tenancy.platform_tx` (actor
  ``platform:<staff sub>``, audit kind ``aa_ops`` in that agency's own audit), called only by ``app.routers.ops``.
"""
