"""The outbound dispatcher (AA-942; lane §2.8 "the single choke point for every outbound send", design §3.5 "Archived
travellers are stopped at the dispatcher", Fix B8 undo delay).

- :mod:`app.dispatch.queue`: queue a message for a traveller (``crm.dispatch_queue`` + one ``crm_dispatch`` job, ids
  only), undo it while it waits.
- :mod:`app.dispatch.worker`: the ``crm_dispatch`` handler, which gates the row **at send time** and hands it to the
  configured sender (CRM-17 e-mail, CRM-28 WhatsApp / SMS). Until a sender exists, a row that passes the gate is
  ``held`` with ``no_sender``.
"""
