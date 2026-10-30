"""The agency's view of a trip's agreements (AA-955, card CRM-25; the trip's Agreements panel, design v3 M12 "100%
signed before departure").

- :func:`trip_agreements`: per traveller of the trip: waiver signed or not, by whom (self / guardian), the insurance
  choice, the envelope SHA-256 and seal algorithm; the booking-terms acceptances (pinned version, price hash); the
  re-accept state (design §2.11 E); the signing log (codes sent and refused, signatures). Ids, codes, hashes and
  times only: no name, no address.
- :func:`envelope`: the sealed envelope of one signature, decrypted (a reveal: a reason code is required and one
  ``reveal`` event is written), with its seal and the seal check (``verified``: true / false, or null when this
  environment cannot check a KMS seal).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from app.errors import Invalid, NotFound, Unavailable
from app.esign import events, terms
from app.esign.envelope import sha256
from app.esign.seal import Seal, Sealer, verify_any
from app.portal import progress
from app.tenancy import TenantContext
from app.travellers.fields import REVEAL_REASONS
from app.vault import Vault

TRIP_SQL = "SELECT trip_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
SIGNED_SQL = """
SELECT request_id, traveler_id, signer_traveler_id, signer_role, insurance_choice, signed_at,
       encode(envelope_sha256, 'hex') AS envelope_sha256, seal_alg, doc_id, terms_doc_id, declination_doc_id
  FROM crm.signature_request
 WHERE trip_id = $1 AND status = 'signed' AND deleted_at IS NULL
 ORDER BY signed_at DESC
"""
REQUEST_SQL = """
SELECT request_id, trip_id FROM crm.signature_request WHERE request_id = $1 AND deleted_at IS NULL
"""


async def trip_agreements(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    done = await progress.compute(ctx, trip_id)
    published = await terms.current(ctx)
    signed: dict[uuid.UUID, Any] = {}
    for r in await ctx.conn.fetch(SIGNED_SQL, trip_id):
        if done.waiver_doc_id is None or r["doc_id"] == done.waiver_doc_id:
            signed.setdefault(r["traveler_id"], r)
    travellers = []
    for t in done.travellers:
        sig = signed.get(t.traveler_id)
        travellers.append(
            {
                "traveler_id": str(t.traveler_id),
                "role": t.role,
                "waiver_signed": t.waiver_signed,
                "signature": None
                if sig is None
                else {
                    "request_id": str(sig["request_id"]),
                    "signed_at": sig["signed_at"].isoformat(),
                    "signer_role": sig["signer_role"],
                    "signer_traveler_id": str(sig["signer_traveler_id"]) if sig["signer_traveler_id"] else None,
                    "insurance_choice": sig["insurance_choice"],
                    "envelope_sha256": sig["envelope_sha256"],
                    "seal_alg": sig["seal_alg"],
                    "with_terms": sig["terms_doc_id"] is not None,
                    "with_declination": sig["declination_doc_id"] is not None,
                },
            }
        )
    newer = await terms.reaccept_needed(ctx, trip_id)
    ctx.audit("view", "allowed", "trip", str(trip_id), reason="agreements")
    return {
        "trip_id": str(trip_id),
        "waiver_required": done.waiver_doc_id is not None,
        "all_signed": done.waiver_doc_id is not None and all(t.waiver_signed for t in done.travellers),
        "travellers": travellers,
        "terms": {
            "published": None
            if published is None
            else {"doc_id": str(published["doc_id"]), "version_label": published["version_label"]},
            "reaccept_required": newer is not None,
            "acceptances": await terms.for_trip(ctx, trip_id),
        },
        "events": await events.for_trip(ctx, trip_id),
    }


async def envelope(
    ctx: TenantContext, vault: Vault, sealer: Sealer | None, request_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    if reason not in REVEAL_REASONS:
        raise Invalid("reason_required", "pick why you open this document", reasons=list(REVEAL_REASONS))
    if await ctx.conn.fetchrow(REQUEST_SQL, request_id) is None:
        raise NotFound("signature_not_found")
    row = await events.envelope_row(ctx, request_id)
    if row is None:
        raise NotFound("envelope_not_found", "this signature has no sealed envelope (signed before e-sign v1)")
    content = await vault.decrypt_bytes("signature_event", "envelope", row["event_id"], bytes(row["envelope_enc"]),
                                        purpose="docs")  # fmt: skip
    digest = sha256(content)
    if digest != bytes(row["envelope_sha256"]):
        ctx.audit("reveal", "denied", "signature_request", str(request_id), fields=("envelope",),
                  reason="digest_mismatch")  # fmt: skip
        raise Unavailable("document_integrity", "the stored envelope does not match its digest")
    seal = Seal(row["seal_alg"], row["seal_key_ref"], bytes(row["seal_value"]))
    verified = await verify_any(sealer, digest, seal)
    ctx.audit("reveal", "allowed", "signature_request", str(request_id), fields=("envelope",), reason=reason)
    return {
        "request_id": str(request_id),
        "envelope_text": content.decode("utf-8"),
        "envelope_sha256": digest.hex(),
        "evidence_sha256": bytes(row["evidence_sha256"]).hex(),
        "signed_at": row["occurred_at"].isoformat(),
        "seal": {
            "alg": seal.alg,
            "key_ref": seal.key_ref,
            "value_b64": base64.b64encode(seal.value).decode("ascii"),
            "verified": verified,
        },
    }
