"""Pass 1 input: the known values of a trip (design v3 §4.5 step 1; lane §5.4 "every identity value tied to the trip").

:func:`known_values_for_trip` opens, through the vault, the identity fields of every live traveller on the trip (lead
and companions: display / given / family name, e-mail, phone, passport number, date of birth) and of their emergency
contacts (the stored text, split into a name and a phone). It runs inside the agency's context (``tenant_tx()`` or a
job's ``system_tx()``), never on a reader role, and leaves one ``scrub_known_values`` audit event naming the travellers
whose fields were opened (ids only). The values exist only in memory until the scrubber has replaced them.

Agency members have no names in the CRM yet (``crm.member`` holds ids and roles) and supplier contacts arrive with
the supplier cards; callers that hold such names pass them as extra :class:`KnownPerson` rows.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from app.privacy.scrubber import KnownPerson, KnownValues
from app.tenancy import AnyContext

PARTY_SQL = """
SELECT t.traveler_id, tt.role, t.display_name_enc, t.given_name_enc, t.family_name_enc, t.email_enc, t.phone_enc,
       t.passport_no_enc, t.dob_enc, t.emergency_contact_enc
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.tenant_id = tt.tenant_id
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL AND t.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, t.traveler_id
"""
FIELDS = ("display_name", "given_name", "family_name", "email", "phone", "passport_no", "dob", "emergency_contact")
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def split_contact(text: str) -> tuple[str | None, tuple[str, ...]]:
    """An emergency contact as stored ("Trần Thị Bình 0912 345 678") -> (name, phones)."""
    phones = tuple(m.group(0) for m in _PHONE_IN_TEXT.finditer(text))
    name = _PHONE_IN_TEXT.sub(" ", text)
    name = re.sub(r"[\s,;:()/|-]+", " ", name).strip()
    return (name or None), phones


async def known_values_for_trip(ctx: AnyContext, vault, trip_id: uuid.UUID) -> KnownValues:  # noqa: ANN001
    rows = await ctx.conn.fetch(PARTY_SQL, trip_id)
    persons: list[KnownPerson] = []
    for row in rows:
        tid = row["traveler_id"]
        values: dict[str, str | None] = {}
        for name in FIELDS:
            blob = row[f"{name}_enc"]
            values[name] = (
                await vault.decrypt("traveler", f"{name}_enc", tid, bytes(blob), tenant_id=ctx.tenant_id)
                if blob is not None
                else None
            )
        dob = None
        if values["dob"]:
            try:
                dob = dt.date.fromisoformat(values["dob"])
            except ValueError:
                dob = None
        persons.append(
            KnownPerson(
                role="traveller" if row["role"] == "lead" else "companion",
                display_name=values["display_name"],
                given_name=values["given_name"],
                family_name=values["family_name"],
                emails=tuple(v for v in (values["email"],) if v),
                phones=tuple(v for v in (values["phone"],) if v),
                passports=tuple(v for v in (values["passport_no"],) if v),
                dobs=(dob,) if dob else (),
            )
        )
        if values["emergency_contact"]:
            name, phones = split_contact(values["emergency_contact"])
            persons.append(KnownPerson(role="emergency_contact", display_name=name, phones=phones))
    if rows:
        ctx.audit(
            "scrub_known_values",
            "allowed",
            "trip",
            str(trip_id),
            fields=FIELDS,
            object_ids=tuple(str(r["traveler_id"]) for r in rows),
        )
    return KnownValues(tuple(persons))
