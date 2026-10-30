"""Module 18 portability (AA-967, card CRM-35): the agency's own data out of the CRM as files it can read and re-use.

- :mod:`app.portability.tables` — which tables and columns an export holds, and how their encrypted columns open.
- :mod:`app.portability.builder` — the ZIP (manifest, JSON + CSV per table, documents and their manifest, SHA256SUMS).
- :mod:`app.portability.exports` — ``crm.export_job`` (crm-026): request, the ``crm_data_export`` job, the short-lived
  download link, withdrawal on erasure / offboarding.
- :mod:`app.portability.requests` — ``crm.data_request`` (crm-026): the agency's data-request log.
"""
