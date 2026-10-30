"""The signing envelope of one traveller (AA-955, card CRM-25; design v3 §5.7 "terms + waiver + insurance decline in
one envelope -> audit page -> seal").

The envelope is canonical UTF-8 text, built the same way every time from the same inputs:

1. a header (the agency, the trip, the documents in it);
2. each document (booking terms, waiver, insurance declination when the traveller declines), its version label and
   the SHA-256 of the version text, then the text with the merge fields filled in (:mod:`app.esign.merge`);
3. the audit page: who signed for whom, the insurance choice, the time, the signing code id and its delivery, the
   SHA-256 of the client address and of the user agent, and the canonical evidence record with its SHA-256.

``envelope_sha256`` = SHA-256 of those bytes; the seal signs it. The evidence record (JSON, sorted keys, no spaces)
holds ids, hashes, codes and times, never a name or an address in clear (the typed name, the client address and the user
agent only as their SHA-256), so it can stay beside the row even after the code purge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.esign import merge

FORMAT = "aa-partner-envelope/1"
KIND_TITLES = {
    "booking_terms": "Booking terms",
    "waiver": "Traveller waiver",
    "insurance_declination": "Travel insurance declination",
}
RULE = "-" * 72


@dataclass(frozen=True)
class Document:
    kind: str
    doc_id: str
    version_label: str
    body_text: str
    body_sha256: bytes

    def rendered(self, values: dict[str, str | None]) -> str:
        return merge.render(self.body_text, values)


def sha256(data: bytes | str) -> bytes:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).digest()


def canonical(record: dict[str, Any]) -> bytes:
    """Sorted keys, no spaces, UTF-8: the bytes every evidence hash covers."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()


def build(
    *,
    agency_name: str,
    trip: dict[str, str | None],
    documents: Sequence[Document],
    values: dict[str, str | None],
    audit: dict[str, Any],
) -> tuple[bytes, bytes, bytes]:
    """(envelope bytes, evidence record, evidence SHA-256). ``audit`` is the evidence record without the documents
    (they are added here: kind, id, version, version hash, rendered hash)."""
    parts = [
        FORMAT,
        f"Agency: {agency_name}",
        f"Trip: {trip.get('title') or '—'} ({trip.get('start_date') or '—'} to {trip.get('end_date') or '—'})",
        f"Documents: {len(documents)}",
    ]
    listed = []
    for i, doc in enumerate(documents, start=1):
        text = doc.rendered(values)
        listed.append(
            {
                "kind": doc.kind,
                "doc_id": doc.doc_id,
                "version_label": doc.version_label,
                "doc_sha256": doc.body_sha256.hex(),
                "rendered_sha256": sha256(text).hex(),
            }
        )
        parts += [
            RULE,
            f"Document {i} of {len(documents)}: {KIND_TITLES.get(doc.kind, doc.kind)}",
            f"Version: {doc.version_label}  SHA-256 of the version: {doc.body_sha256.hex()}",
            RULE,
            text,
        ]
    record = canonical({**audit, "format": FORMAT, "documents": listed})
    digest = sha256(record)
    parts += [
        RULE,
        "Audit page",
        RULE,
        f"Signed for: traveller {audit.get('traveler_id')} ({audit.get('traveller_role')})",
        f"Signed by: {values.get('signer.name') or '—'} ({audit.get('signer_role')}, traveller "
        f"{audit.get('signer_traveler_id')})",
        f"Travel insurance: {audit.get('insurance_choice') or 'not asked'}",
        f"Signed at (UTC): {audit.get('signed_at')}",
        f"Signing code: {audit.get('otp_id')} (delivery: {audit.get('code_delivery')})",
        f"Client address SHA-256: {audit.get('client_ip_sha256') or '—'}",
        f"User agent SHA-256: {audit.get('user_agent_sha256') or '—'}",
        "Evidence record:",
        record.decode(),
        f"Evidence SHA-256: {digest.hex()}",
        "",
    ]
    return "\n".join(parts).encode(), record, digest


def optional_sha256(value: str | None) -> str | None:
    return sha256(value).hex() if value else None
