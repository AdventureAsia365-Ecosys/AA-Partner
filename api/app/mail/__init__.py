"""Module 5 e-mail (AA-945, design v3 card CRM-17; lane §6.5; BYO errata S247 §1-§3).

- **Receive** (forward-in): the agency forwards (or BCCs) its mail to ``<local_part>@<CRM_INGEST_DOMAIN>``; AA's SES
  receipt rule stores the raw message under ``raw/`` of the ingest bucket (<= 1 day). :mod:`app.mail.ingest` parses it
  (:mod:`app.mail.parse`), finds the agency by the local part (``app.tenancy.ingest_tx``), threads it
  (:mod:`app.mail.threading`), re-encrypts it under the agency's ``mail`` DEK, keeps the allowed attachments as
  documents and deletes the raw object. An unknown address is discarded (count-only log, card R).
- **Send** (send-as): a message to a client leaves the agency's own mailbox (Gmail, Microsoft 365 or SMTP,
  :mod:`app.mail.providers`) through the dispatcher (:mod:`app.mail.sender`, the crm-010 ``Sender``). A dead token or a
  failed SMTP login holds the message ``held_reconnect``; it is never sent through AA's SES (card R). The sender table
  (:mod:`app.mail.senders`) says which path each kind of mail takes.
- **Work**: conversations, the Unmatched inbox, drafts (autosaved), Undo, saved replies (:mod:`app.mail.conversations`),
  the mailbox settings (:mod:`app.mail.mailbox`, :mod:`app.mail.oauth`), the nightly health check
  (:mod:`app.mail.health`) and the mbox / CSV history import (:mod:`app.mail.history_import`).
"""
