"""The agency's view of a trip's agreements (AA-955, card CRM-25; the trip's Agreements panel, design v3 M12 "100%
signed before departure").

- :func:`trip_agreements`: per traveller of the trip: waiver signed or not, by whom (self / guardian), the insurance
  choice, the envelope SHA-256 and seal algorithm; the booking-terms acceptances (pinned version, price hash); the
  re-accept state (design §2.11 E); the signing log (codes sent and refused, signatures). Ids, codes, hashes and
  times only: no name, no address.
- :func:`envelope`: the sealed envelope of one signature, decrypted (a reveal: a reason code is required and one
  ``reveal`` event is written), with its seal and the seal check (``verified``: true / false, or null when this
  environment cannot check a KMS seal).
- :func:`envelope_pdf` (AA-1012): the same envelope as a PAdES-B-B signed PDF (a reveal too); :func:`signed_pdf`
  builds it for the agency and for the traveller portal (:func:`app.portal.public.agreement_pdf`). A seal KMS rejects
  never becomes a PDF (503 ``document_integrity``).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.errors import Invalid, NotFound, Unavailable
from app.esign import events, terms
from app.esign.envelope import sha256
from app.esign.pades import Attachment, PadesSealer, require_pades
from app.esign.seal import KMS_ALG, Seal, Sealer, verify_any
from app.portal import progress
from app.tenancy import AnyContext, TenantContext
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
AGENCY_NAME_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
PDF_REASON = "Signed agreement sealed by the platform"


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


@dataclass(frozen=True)
class Opened:
    row: Any
    content: bytes
    digest: bytes
    seal: Seal
    verified: bool | None


async def _open(ctx: AnyContext, vault: Vault, sealer: Sealer | None, request_id: uuid.UUID, action: str) -> Opened:
    """The sealed envelope of a signature, decrypted after its stored digest matched; ``action`` names the denied
    audit event of a digest mismatch."""
    row = await events.envelope_row(ctx, request_id)
    if row is None:
        raise NotFound("envelope_not_found", "this signature has no sealed envelope (signed before e-sign v1)")
    content = await vault.decrypt_bytes("signature_event", "envelope", row["event_id"], bytes(row["envelope_enc"]),
                                        purpose="docs")  # fmt: skip
    digest = sha256(content)
    if digest != bytes(row["envelope_sha256"]):
        ctx.audit(action, "denied", "signature_request", str(request_id), fields=("envelope",),
                  reason="digest_mismatch")  # fmt: skip
        raise Unavailable("document_integrity", "the stored envelope does not match its digest")
    seal = Seal(row["seal_alg"], row["seal_key_ref"], bytes(row["seal_value"]))
    return Opened(row, content, digest, seal, await verify_any(sealer, digest, seal))


async def envelope(
    ctx: TenantContext, vault: Vault, sealer: Sealer | None, request_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    if reason not in REVEAL_REASONS:
        raise Invalid("reason_required", "pick why you open this document", reasons=list(REVEAL_REASONS))
    if await ctx.conn.fetchrow(REQUEST_SQL, request_id) is None:
        raise NotFound("signature_not_found")
    opened = await _open(ctx, vault, sealer, request_id, "reveal")
    row, seal = opened.row, opened.seal
    ctx.audit("reveal", "allowed", "signature_request", str(request_id), fields=("envelope",), reason=reason)
    return {
        "request_id": str(request_id),
        "envelope_text": opened.content.decode("utf-8"),
        "envelope_sha256": opened.digest.hex(),
        "evidence_sha256": bytes(row["evidence_sha256"]).hex(),
        "signed_at": row["occurred_at"].isoformat(),
        "seal": {
            "alg": seal.alg,
            "key_ref": seal.key_ref,
            "value_b64": base64.b64encode(seal.value).decode("ascii"),
            "verified": opened.verified,
        },
    }


async def envelope_pdf(
    ctx: TenantContext,
    vault: Vault,
    sealer: Sealer | None,
    pades: PadesSealer | None,
    request_id: uuid.UUID,
    reason: str,
) -> dict[str, Any]:
    """The agency's download of a signature's PDF: a reveal (reason code, one ``reveal`` event, field
    ``envelope_pdf``)."""
    if reason not in REVEAL_REASONS:
        raise Invalid("reason_required", "pick why you open this document", reasons=list(REVEAL_REASONS))
    if await ctx.conn.fetchrow(REQUEST_SQL, request_id) is None:
        raise NotFound("signature_not_found")
    out = await signed_pdf(ctx, vault, sealer, pades, request_id, action="reveal")
    ctx.audit("reveal", "allowed", "signature_request", str(request_id), fields=("envelope_pdf",), reason=reason)
    return out


def _key_id(seal: Seal) -> str:
    """The KMS key id of a seal without the account part of its ARN (the PDF goes to travellers)."""
    return seal.key_ref.rsplit("/", 1)[-1] if seal.alg == KMS_ALG else seal.key_ref


async def signed_pdf(
    ctx: AnyContext,
    vault: Vault,
    sealer: Sealer | None,
    pades: PadesSealer | None,
    request_id: uuid.UUID,
    *,
    action: str,
) -> dict[str, Any]:
    """``{filename, content_type, content_b64, envelope_sha256, seal_verified}``: the envelope printed by
    :func:`app.documents.renders.signed_agreement` (the agency's name and locale), with ``envelope.txt`` (the sealed
    bytes) and ``seal.json`` embedded, signed PAdES-B-B with the seal key. The caller writes the allowed audit event."""
    from app.documents import renders
    from app.proposals.prep_packs import agency_locale

    signer = require_pades(pades)
    opened = await _open(ctx, vault, sealer, request_id, action)
    if opened.verified is False:
        ctx.audit(action, "denied", "signature_request", str(request_id), fields=("envelope",), reason="seal_rejected")
        raise Unavailable("document_integrity", "the stored seal does not match the envelope")
    row, seal = opened.row, opened.seal
    view = {
        "agency_name": await ctx.conn.fetchval(AGENCY_NAME_SQL, ctx.tenant_id),
        "envelope_text": opened.content.decode("utf-8"),
        "envelope_sha256": opened.digest.hex(),
        "signed_at": row["occurred_at"].isoformat(),
        "seal_alg": seal.alg,
        "seal_verified": opened.verified,
    }
    pdf = await asyncio.to_thread(renders.signed_agreement, view, await agency_locale(ctx))  # type: ignore[arg-type]
    seal_record = {
        "format": "aa-partner-seal/1",
        "envelope_sha256": opened.digest.hex(),
        "evidence_sha256": bytes(row["evidence_sha256"]).hex(),
        "signed_at": row["occurred_at"].isoformat(),
        "seal_alg": seal.alg,
        "seal_key_id": _key_id(seal),
        "seal_value_b64": base64.b64encode(seal.value).decode("ascii"),
    }
    signed = await signer.sign(
        pdf,
        attachments=(
            Attachment("envelope.txt", opened.content, "text/plain", "The sealed envelope (UTF-8)"),
            Attachment(
                "seal.json",
                json.dumps(seal_record, sort_keys=True, indent=2).encode(),
                "application/json",
                "The envelope's seal",
            ),  # fmt: skip
        ),
        reason=PDF_REASON,
    )
    return {
        "request_id": str(request_id),
        "filename": f"signed-agreement-{str(request_id)[:8]}.pdf",
        "content_type": "application/pdf",
        "content_b64": base64.b64encode(signed).decode("ascii"),
        "envelope_sha256": opened.digest.hex(),
        "seal_verified": opened.verified,
    }
