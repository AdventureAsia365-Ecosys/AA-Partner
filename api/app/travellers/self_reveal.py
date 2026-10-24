"""``traveller_self_reveal(session)``: what a traveller may see of their **own** record (design v3 S6, lane §5.5).

The client portal (CRM-24a, AA-953) signs a traveller in per person (one-time code) and opens a session bound to one
agency, one trip and one traveller (:class:`TravellerSession`). The public / client routes may call this function and
never the agency's generic :func:`app.travellers.reveal.reveal` (``tests/unit/test_reveal_boundary.py``; this module
does not import it either).

Rules:

- the session's agency must be the context's agency, and the traveller must be on the session's trip (a companion's
  session never opens the lead's record, and the reverse);
- names, e-mail, phone and DOB come back in full (it is the traveller's own data, they typed it);
- the passport number is **write-only**: only its last three characters (``"•••• 123"``); expiry and country are shown;
- no health codes, no emergency contact, no relationships, no notes, no other traveller;
- one ``audit_event`` per call, actor ``traveler:<id>`` (actor kind ``traveler``), the fields shown.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.audit import AuditEvent, current_request_id
from app.errors import Forbidden, NotFound
from app.tenancy import AnyContext
from app.travellers.fields import mask_passport
from app.vault import Vault

SELF_FIELDS = (
    "display_name",
    "given_name",
    "family_name",
    "email",
    "phone",
    "dob",
    "passport_expiry",
    "passport_country",
)
SELF_SQL = """
SELECT tr.traveler_id, tr.display_name_enc, tr.given_name_enc, tr.family_name_enc, tr.email_enc, tr.phone_enc,
       tr.dob_enc, tr.passport_no_enc, tr.passport_expiry_enc, tr.passport_country_enc
  FROM crm.traveler tr
  JOIN crm.trip_traveler tt ON tt.traveler_id = tr.traveler_id AND tt.deleted_at IS NULL
 WHERE tr.traveler_id = $1 AND tt.trip_id = $2 AND tr.deleted_at IS NULL
"""


@dataclass(frozen=True)
class TravellerSession:
    """A signed-in traveller (client portal, CRM-24a): the agency, the trip and the person the code was sent to."""

    tenant_id: uuid.UUID
    trip_id: uuid.UUID
    traveler_id: uuid.UUID
    access: str  # lead | companion


async def traveller_self_reveal(ctx: AnyContext, vault: Vault, session: TravellerSession) -> dict[str, Any]:
    if session.tenant_id != ctx.tenant_id or session.access not in ("lead", "companion"):
        raise Forbidden("session_mismatch")
    row = await ctx.conn.fetchrow(SELF_SQL, session.traveler_id, session.trip_id)
    if row is None:
        raise NotFound("traveler_not_found")
    out: dict[str, Any] = {"traveler_id": str(session.traveler_id)}
    for name in SELF_FIELDS:
        blob = row[f"{name}_enc"]
        out[name] = (
            await vault.decrypt("traveler", f"{name}_enc", session.traveler_id, blob, tenant_id=ctx.tenant_id)
            if blob is not None
            else None
        )
    passport = row["passport_no_enc"]
    out["passport_no_masked"] = mask_passport(
        await vault.decrypt("traveler", "passport_no_enc", session.traveler_id, passport, tenant_id=ctx.tenant_id)
        if passport is not None
        else None
    )
    ctx.audit_events.append(
        AuditEvent(
            actor_kind="traveler",
            actor_ref=f"traveler:{session.traveler_id}",
            action="self_view",
            outcome="allowed",
            object_kind="traveler",
            object_id=str(session.traveler_id),
            fields=(*SELF_FIELDS, "passport_no_masked"),
            request_id=current_request_id(),
        )
    )
    return out
