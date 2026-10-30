"""Merge fields of an agreement text (AA-955, card CRM-25; design v3 A-16 Library "terms/waivers").

An agency writes ``{{field}}`` in a booking terms, waiver or insurance declination version; the draft refuses any name
outside :data:`MERGE_FIELDS` (422 ``unknown_merge_field``) and the signing page renders the version for the signer.
Only trip and signing facts are offered: no traveller field (passport, birth date, address) is ever merged into a
document, so rendering decrypts nothing. ``signer.name`` is the full name the signer types at signing.

Before signing the portal shows :data:`PREVIEW` placeholders for the signing facts; the sealed envelope holds the
final values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

FIELD = re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z_]+)?)\s*\}\}")
MERGE_FIELDS: dict[str, str] = {
    "agency.name": "the agency's display name",
    "trip.title": "the trip title",
    "trip.start_date": "the trip start date (YYYY-MM-DD)",
    "trip.end_date": "the trip end date (YYYY-MM-DD)",
    "traveller.role": "lead traveller or companion (the person the document is for)",
    "signer.name": "the full name the signer types",
    "signer.role": "traveller, or guardian of a minor",
    "signed.date": "the signing date (YYYY-MM-DD, UTC)",
}
PREVIEW: dict[str, str] = {
    "signer.name": "[your full name]",
    "signed.date": "[signing date]",
}


def unknown_fields(text: str) -> list[str]:
    """The merge field names in ``text`` that are not offered, sorted, each once."""
    return sorted({m.group(1) for m in FIELD.finditer(text)} - set(MERGE_FIELDS))


def render(text: str, values: Mapping[str, str | None]) -> str:
    """``text`` with every known field replaced by its value (an empty value -> ``—``); an unknown name stays as
    written (the draft check refuses them; an old version keeps what it said)."""

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in MERGE_FIELDS:
            return match.group(0)
        value = values.get(name)
        return value if value else "—"

    return FIELD.sub(one, text)
