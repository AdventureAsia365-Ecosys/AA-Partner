"""``reveal(field, reason)``: open one F1 field of one traveller for an agency member (design v3 §4.4, §4.9).

The only agency path to a field beyond the display name. Every call queues **one** ``audit_event`` (action ``reveal``,
object the traveller, the field name, the reason code) in the same ``tenant_tx()`` transaction: when the event cannot
be written the request fails and the value is never returned (lane §5.7 "if the write fails, the read fails"). Every
role may reveal (design §4.6: owner, agent and assistant, each logged).

Public routes never import this module (``tests/unit/test_reveal_boundary.py``, card CRM-09 R test): a traveller sees
their own record through :func:`app.travellers.self_reveal.traveller_self_reveal` only.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.errors import Conflict, Invalid, NotFound
from app.tenancy import TenantContext
from app.travellers.fields import HEALTH_FIELD, REVEAL_COLUMNS, REVEAL_FIELDS, REVEAL_REASONS
from app.vault import Vault

TRAVELER_SQL = """
SELECT traveler_id, given_name_enc, family_name_enc, email_enc, phone_enc, dob_enc, nationality_enc, passport_no_enc,
       passport_expiry_enc, passport_country_enc, emergency_contact_enc
  FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL
"""
HEALTH_SQL = """
SELECT health_id, health_flags_enc FROM crm.traveler_health WHERE traveler_id = $1 AND deleted_at IS NULL
"""
# Consent rows are append-only; the latest health_data row decides (design §4.3 "health ... with consent").
HEALTH_CONSENT_SQL = """
SELECT value FROM crm.consent WHERE traveler_id = $1 AND purpose = 'health_data' ORDER BY at DESC, consent_id LIMIT 1
"""


async def reveal(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID, field: str, reason: str) -> dict[str, Any]:
    """``{"traveler_id", "field", "value"}``; ``value`` is ``None`` when the field is empty (still audited: someone
    looked). Health codes come back as ``{"dietary": [...], "mobility": [...], "medical": [...]}``, and only while the
    traveller's latest ``health_data`` consent is true (else 409 ``health_consent_withdrawn`` and a denied event)."""
    if field not in REVEAL_FIELDS:
        raise Invalid("invalid_field", f"one of {', '.join(REVEAL_FIELDS)}")
    if reason not in REVEAL_REASONS:
        raise Invalid("reason_required", "pick why you open this field", reasons=list(REVEAL_REASONS))
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None:
        raise NotFound("traveler_not_found")
    value: Any
    if field == HEALTH_FIELD:
        health = await ctx.conn.fetchrow(HEALTH_SQL, traveler_id)
        value = None
        if health is not None:
            if await ctx.conn.fetchval(HEALTH_CONSENT_SQL, traveler_id) is not True:
                # Withdrawn (or never given): the codes stay sealed until the withdrawal flow clears them (CRM-14).
                ctx.audit("reveal", "denied", "traveler", str(traveler_id), fields=(field,), reason="consent_withdrawn")
                raise Conflict("health_consent_withdrawn", "the traveller withdrew the health data consent")
            value = json.loads(
                await vault.decrypt(
                    "traveler_health", "health_flags_enc", health["health_id"], health["health_flags_enc"]
                )
            )
    else:
        column = REVEAL_COLUMNS[field]
        blob = row[column]
        value = (
            await vault.decrypt("traveler", column, traveler_id, blob, tenant_id=ctx.tenant_id)
            if blob is not None
            else None
        )
    ctx.audit("reveal", "allowed", "traveler", str(traveler_id), fields=(field,), reason=reason)
    return {"traveler_id": str(traveler_id), "field": field, "value": value}
