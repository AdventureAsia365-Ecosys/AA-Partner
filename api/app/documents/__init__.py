"""Agency documents (AA-937, design v3 §4.1 Fix A7; crm-006 ``crm.document``).

- :mod:`app.documents.store`: where the encrypted objects live (:class:`S3DocumentStore` on the documents bucket,
  :class:`MemoryDocumentStore` in tests and local runs).
- :mod:`app.documents.service`: upload (encrypt with the tenant ``docs`` DEK before the PUT), list, download (one
  ``reveal`` audit event, field ``document``), trash.
"""
