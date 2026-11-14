"""The traveller's signed agreement PDF (AA-1012, card CRM-25b): the PAdES-B-B PDF of a signed envelope, downloaded
from the portal (``GET /crm/public/v1/traveller/agreements/{traveler_id}/pdf``).

Who may download: the signed-in traveller (the person the session acts as, :func:`app.portal.public._signed_in`) for
their own envelope, whoever signed it (a minor's own included), or for an envelope they signed as guardian; anyone else
is 403 ``envelope_not_yours``. The PDF itself is :func:`app.esign.service.signed_pdf`. Kept out of
:mod:`app.portal.public` (the code-health file-size ratchet), it reuses that module's link and session checks.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app.errors import Forbidden, NotFound
from app.esign import service as esign_service
from app.esign.pades import PadesSealer
from app.esign.seal import Sealer
from app.portal import public as portal
from app.tenancy import client_link_tx
from app.vault import KeyProvider, Vault

# The newest signed envelope of a traveller of the trip, and who signed it.
SIGNED_ENVELOPE_SQL = """
SELECT request_id, traveler_id, signer_traveler_id FROM crm.signature_request
 WHERE trip_id = $1 AND traveler_id = $2 AND status = 'signed' AND deleted_at IS NULL
 ORDER BY signed_at DESC LIMIT 1
"""


async def agreement_pdf(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    token: str,
    session: str | None,
    traveler_id: Any,
    *,
    sealer: Sealer | None,
    pades: PadesSealer | None,
) -> dict[str, Any]:
    """The PDF for the signed-in traveller; 404 ``signature_not_found`` when the traveller has no signed envelope on
    this trip (or the id is not one)."""
    try:
        wanted = uuid.UUID(str(traveler_id))
    except ValueError:
        raise NotFound("signature_not_found") from None
    async with client_link_tx(pool, portal.hash_hex(token), purpose=portal.PURPOSE) as (ctx, link):
        access = await portal._signed_in(ctx, await portal._access(ctx, link), session)
        row = await ctx.conn.fetchrow(SIGNED_ENVELOPE_SQL, access["trip_id"], wanted)
        if row is None:
            raise NotFound("signature_not_found")
        me = access["traveler_id"]
        if row["traveler_id"] != me and row["signer_traveler_id"] != me:
            ctx.audit("portal_document", "denied", "signature_request", str(row["request_id"]),
                      reason="envelope_not_yours")  # fmt: skip
            raise Forbidden("envelope_not_yours", "you can download your own signed documents")
        out = await esign_service.signed_pdf(
            ctx,
            Vault(keys, ctx),  # type: ignore[arg-type]
            sealer,
            pades,
            row["request_id"],
            action="portal_document",
        )
        ctx.audit("portal_document", "allowed", "signature_request", str(row["request_id"]),
                  fields=("envelope_pdf",), object_ids=(str(access["access_id"]),))  # fmt: skip
        return out
