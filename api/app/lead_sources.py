"""The lead source codes the API writes on ``crm.trip.lead_source`` and ``crm.traveler.lead_source`` (AA-957 S262 round
3: the trip card printed the raw code "synthetic_seed").

The database keeps a bare code (crm-003 / crm-005 ``*_lead_source_chk``: ``^[a-z][a-z0-9_]{0,39}$``, never free text),
and the web words it (``source.<code>`` in ``web/lib/i18nTripClosed.ts``). This file is the list both sides check
against:

- ``tests/unit/test_lead_sources.py`` reads the API source and fails when a module writes a code that is not listed
  here (or one listed here is no longer written);
- ``web/tests/closedTripRound3.test.ts`` ("lead sources") reads this file and fails when a listed code has no EN or VI
  label.

A code written by an API caller (``POST /travelers`` takes any code) or a form created outside the app reads as words
on the web ("facebook_ads" -> "Facebook ads"), never the raw code.
"""

from __future__ import annotations

LEAD_SOURCES: tuple[str, ...] = (
    "agent_created",  # a client added by a member (POST /travelers without a code)
    "companion",  # a companion added to a trip by name
    "import",  # the CSV import
    "synthetic_seed",  # the Dev / demo seed's inquiry form
    "website_form",  # an inquiry form (the default code of a new form)
)
