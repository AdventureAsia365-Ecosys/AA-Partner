"""Module 9: SMS and WhatsApp through the agency's own Twilio account (AA-958, card CRM-28; design v3 M9, §5.4, WF-11;
BYO errata S247 §1 / §2).

- :mod:`app.messaging.providers`: the provider interface, Twilio, and the deterministic sandbox stub (dev only);
- :mod:`app.messaging.connection`: Settings > Channels (connect, disconnect, view);
- :mod:`app.messaging.health`: the Test button, the template sync and the daily check;
- :mod:`app.messaging.compose`: what the composer may offer and the send (409 ``template_required``);
- :mod:`app.messaging.sender`: the dispatcher's sender for ``sms`` / ``whatsapp`` and the channel router;
- :mod:`app.messaging.webhook`: inbound messages and status callbacks (signature first, 403 otherwise);
- :mod:`app.messaging.signature`: the Twilio request signature;
- :mod:`app.messaging.events`: ``message.received`` / ``message.sent`` (the workflow seam);
- :mod:`app.messaging.state`: the runtime, the window, the opt-out and the connection's failure handling.

Messages live in the AA-945 tables (``crm.conversation``, ``crm.message`` with channel ``sms`` / ``whatsapp``), so the
trip Messages tab, the Unmatched inbox, Link, Undo and the erasure paths are shared with e-mail.
"""
