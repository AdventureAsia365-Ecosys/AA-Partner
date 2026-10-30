"""Who signs which envelope, and what is in it (AA-955, card CRM-25; design v3 §5.7, O-6 "each adult signs their own
waiver; the lead signs as guardian for minors", R13 "travellers who decline sign the declination in the waiver").

- **Documents** (:func:`documents`): the published waiver (required: no waiver, nothing to sign), the published
  booking terms when there is one, and the published insurance declination when the traveller declines insurance.
  The signer must send back the id and SHA-256 of each one exactly as shown (409 ``document_changed`` otherwise).
- **Minors** (:func:`minors`): the travellers of the trip under 18 on the trip's start date, read from their DOB (F1,
  decrypted here only to compare it with the start date; one audit event per read, field ``dob``). A traveller
  without a DOB on file, or a trip without a start date, counts as an adult (Stage 3 asks for the DOB, so a minor's
  details are complete only once the DOB is known).
- **Who may sign** (:func:`envelopes`): an adult signs their own envelope; a minor never signs (403
  ``guardian_signs``); the lead, when an adult, signs as guardian for each minor of the trip.

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from app.esign import merge
from app.esign.envelope import Document
from app.tenancy import AnyContext
from app.travellers.age_bands import age_on
from app.vault import Vault

ADULT_AGE = 18
INSURANCE_CHOICES = ("insured", "declined")

PUBLISHED_SQL = """
SELECT doc_id, kind, version_label, body_text, body_sha256 FROM crm.agreement_doc
 WHERE status = 'published' AND deleted_at IS NULL
   AND kind IN ('booking_terms', 'waiver', 'insurance_declination')
"""
PARTY_DOB_SQL = """
SELECT tt.traveler_id, tt.role, t.dob_enc FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
SIGNATURES_SQL = """
SELECT traveler_id, request_id, status, signed_at, signer_role, insurance_choice FROM crm.signature_request
 WHERE trip_id = $1 AND doc_id = $2 AND status <> 'void' AND deleted_at IS NULL
"""


@dataclass(frozen=True)
class Published:
    waiver: Document | None
    terms: Document | None
    declination: Document | None


@dataclass(frozen=True)
class PartyMember:
    traveler_id: uuid.UUID
    role: str
    minor: bool


def _doc(row: Any) -> Document:
    return Document(
        kind=row["kind"],
        doc_id=str(row["doc_id"]),
        version_label=row["version_label"],
        body_text=row["body_text"],
        body_sha256=bytes(row["body_sha256"]),
    )


async def published(ctx: AnyContext) -> Published:
    rows = {r["kind"]: _doc(r) for r in await ctx.conn.fetch(PUBLISHED_SQL)}
    return Published(rows.get("waiver"), rows.get("booking_terms"), rows.get("insurance_declination"))


def documents(found: Published, insurance: str | None) -> list[Document]:
    """The envelope's documents in their fixed order: booking terms, waiver, declination (when declined)."""
    out = [d for d in (found.terms, found.waiver) if d is not None]
    if insurance == "declined" and found.declination is not None:
        out.append(found.declination)
    return out


async def party(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, start: dt.date | None) -> list[PartyMember]:
    rows = await ctx.conn.fetch(PARTY_DOB_SQL, trip_id)
    out = []
    read = []
    for r in rows:
        minor = False
        if start is not None and r["dob_enc"] is not None:
            dob = dt.date.fromisoformat(await vault.decrypt("traveler", "dob_enc", r["traveler_id"], r["dob_enc"]))
            read.append(str(r["traveler_id"]))
            minor = dob <= start and age_on(dob, start) < ADULT_AGE
        out.append(PartyMember(r["traveler_id"], r["role"], minor))
    if read:
        ctx.audit("guardian_check", "allowed", "trip", str(trip_id), fields=("dob",), object_ids=tuple(sorted(read)))
    return out


def envelopes(members: list[PartyMember], me: uuid.UUID) -> list[PartyMember]:
    """The envelopes the traveller ``me`` signs: their own when an adult, and every minor's when they are the adult
    lead. A minor signs none."""
    mine = next((m for m in members if m.traveler_id == me), None)
    if mine is None or mine.minor:
        return []
    out = [mine]
    if mine.role == "lead":
        out += [m for m in members if m.minor]
    return out


def preview_values(agency: str | None, trip: Any, role: str) -> dict[str, str | None]:
    return {
        "agency.name": agency,
        "trip.title": trip["title"],
        "trip.start_date": trip["start_date"].isoformat() if trip["start_date"] else None,
        "trip.end_date": trip["end_date"].isoformat() if trip["end_date"] else None,
        "traveller.role": "lead traveller" if role == "lead" else "companion",
        "signer.role": None,
        **merge.PREVIEW,
    }


def document_view(doc: Document, values: dict[str, str | None]) -> dict[str, Any]:
    return {
        "kind": doc.kind,
        "doc_id": doc.doc_id,
        "version_label": doc.version_label,
        "body_text": doc.rendered(values),
        "body_sha256": doc.body_sha256.hex(),
    }


def shown_hashes(raw: Any) -> dict[str, str]:
    """``{doc_id: sha256 hex}`` from the request: ``documents: [{doc_id, doc_sha256}]``, or the v0 single waiver
    (``doc_id`` + ``doc_sha256``)."""
    out: dict[str, str] = {}
    items = raw.get("documents") if isinstance(raw.get("documents"), list) else None
    if items is None and raw.get("doc_id") is not None:
        items = [{"doc_id": raw.get("doc_id"), "doc_sha256": raw.get("doc_sha256")}]
    for item in (items or [])[:5]:
        if isinstance(item, dict) and isinstance(item.get("doc_id"), str) and isinstance(item.get("doc_sha256"), str):
            out[item["doc_id"]] = item["doc_sha256"]
    return out


async def signatures(ctx: AnyContext, trip_id: uuid.UUID, waiver_id: str) -> dict[uuid.UUID, Any]:
    return {r["traveler_id"]: r for r in await ctx.conn.fetch(SIGNATURES_SQL, trip_id, uuid.UUID(waiver_id))}
