"""Data lifecycle of an agency (AA-942, design v3 card CRM-14; §3.5 "Trash, delete block, archive", §3.6 retention,
§4.10 erasure): retention rules per data class, the trash, the delete block, archive, legal holds, the daily
``crm_lifecycle`` job, erasure and agency offboarding.

Every hard delete goes through one database function, ``crm.erase_subject`` (crm-010), reached as ``crm_eraser``
(:func:`app.tenancy.as_eraser`); :mod:`app.lifecycle.erase` is its only caller.
"""
