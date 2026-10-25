"""CRM-19 (AA-947): the cache of published AA tour content from AA-CIS-App ``catalog.http.v1`` (AA-976).

- :mod:`app.catalog.client`: the M2M HTTP client (scope ``cis.catalog/read``, ETag / If-None-Match).
- :mod:`app.catalog.store`: the platform tables ``crm.aa_catalog_tour`` / ``crm.aa_catalog_state`` (no tenant rows).
- :mod:`app.catalog.sync`: the hourly ``crm_catalog_sync`` and its due-now schedule.

Agencies read the cache through their own transaction (:func:`app.catalog.store.search` on ``ctx.conn``); only the
sync writes it.
"""
