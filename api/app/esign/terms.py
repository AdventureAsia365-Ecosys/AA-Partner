"""Booking-terms acceptances (AA-955, card CRM-25; design v3 M12, R34, §2.11 E "terms updated after viewing ->
re-accept before payment").

One ``crm.terms_acceptance`` row per acceptance of a booking terms version, pinned to the version, its SHA-256 and
the proposal's ``price_hash``:

- ``proposal``: the client accepted a proposal that pins a terms version (:func:`app.proposals.public.accept`);
- ``reaccept``: the agency published a newer booking terms version after that, and the client accepted the new one on
  the same proposal page (:func:`app.proposals.public.reaccept_terms`);
- ``envelope``: a traveller accepted the terms inside their signing envelope (:func:`app.portal.public.sign`).

**The re-accept rule** (:func:`reaccept_needed`): a trip whose accepted proposal's deposit is not paid yet, while the
agency has a published booking terms version that nobody on the trip accepted: the client must accept that version
before the deposit is recorded (``mark-paid`` answers 409 ``terms_reaccept_required``), and the proposal page shows
the new version. A deposit already paid is never reopened.

Every row keeps its evidence record (ids, hashes, time; no PII) and the client address and user agent under the vault
DEK. It has no foreign key on the trip, proposal, traveller or link (DM-7 claim-period exception: it outlives their
erasure until ``keep_until``).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.errors import NotFound
from app.esign.envelope import canonical, optional_sha256, sha256
from app.tenancy import AnyContext
from app.vault import Vault

MAX_USER_AGENT = 400

CURRENT_SQL = """
SELECT doc_id, version_label, body_text, body_sha256, published_at FROM crm.agreement_doc
 WHERE kind = 'booking_terms' AND status = 'published' AND deleted_at IS NULL
"""
# keep_until: the trip end (else start, else today) + 3 years (design §3.6, est.; counsel confirms).
INSERT_SQL = """
INSERT INTO crm.terms_acceptance (acceptance_id, trip_id, proposal_id, traveler_id, link_id, request_id, source, doc_id,
                                  doc_sha256, price_hash, typed_name_sha256, client_ip_enc, user_agent_enc,
                                  evidence_record, evidence_sha256, accepted_at, keep_until)
SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
       greatest((coalesce(t.end_date, t.start_date, ($16::timestamptz AT TIME ZONE 'UTC')::date)
                 + interval '3 years')::date, ($16::timestamptz AT TIME ZONE 'UTC')::date)
  FROM crm.trip t WHERE t.trip_id = $2
RETURNING acceptance_id
"""
REACCEPT_SQL = """
SELECT p.proposal_id, p.price_hash, p.link_id,
       EXISTS (SELECT 1 FROM crm.terms_acceptance a WHERE a.trip_id = p.trip_id AND a.doc_id = $2) AS accepted,
       EXISTS (SELECT 1 FROM crm.payment_schedule s
                WHERE s.proposal_id = p.proposal_id AND s.kind = 'deposit' AND s.status = 'paid'
                  AND s.deleted_at IS NULL) AS deposit_paid
  FROM crm.proposal p
 WHERE p.trip_id = $1 AND p.status = 'accepted' AND p.deleted_at IS NULL
"""
TRIP_ACCEPTANCES_SQL = """
SELECT a.acceptance_id, a.source, a.doc_id, d.version_label, a.proposal_id, a.traveler_id, a.request_id, a.accepted_at,
       encode(a.evidence_sha256, 'hex') AS evidence_sha256, encode(a.doc_sha256, 'hex') AS doc_sha256,
       encode(a.price_hash, 'hex') AS price_hash
  FROM crm.terms_acceptance a JOIN crm.agreement_doc d ON d.doc_id = a.doc_id
 WHERE a.trip_id = $1
 ORDER BY a.accepted_at DESC, a.acceptance_id
 LIMIT 50
"""


def clean_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    text = "".join(ch for ch in value if ch.isprintable())[:MAX_USER_AGENT].strip()
    return text or None


async def current(ctx: AnyContext) -> Any:
    """The published booking terms version, or None."""
    return await ctx.conn.fetchrow(CURRENT_SQL)


async def reaccept_needed(ctx: AnyContext, trip_id: uuid.UUID) -> Any:
    """The published booking terms version the client still has to accept before the deposit, or None."""
    terms = await current(ctx)
    if terms is None:
        return None
    row = await ctx.conn.fetchrow(REACCEPT_SQL, trip_id, terms["doc_id"])
    if row is None or row["deposit_paid"] or row["accepted"]:
        return None
    return terms


async def record(
    ctx: AnyContext,
    vault: Vault,
    *,
    trip_id: uuid.UUID,
    source: str,
    doc: Any,
    typed_name: str,
    at: Any,
    proposal_id: uuid.UUID | None = None,
    price_hash: bytes | None = None,
    traveler_id: uuid.UUID | None = None,
    link_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> uuid.UUID:
    """Write one acceptance of ``doc`` (an ``agreement_doc`` row: doc_id, body_sha256). Returns its id."""
    acceptance_id = uuid.uuid4()
    agent = clean_user_agent(user_agent)
    evidence = canonical(
        {
            "acceptance_id": str(acceptance_id),
            "source": source,
            "trip_id": str(trip_id),
            "proposal_id": str(proposal_id) if proposal_id else None,
            "traveler_id": str(traveler_id) if traveler_id else None,
            "link_id": str(link_id) if link_id else None,
            "request_id": str(request_id) if request_id else None,
            "doc_id": str(doc["doc_id"]),
            "doc_sha256": bytes(doc["body_sha256"]).hex(),
            "price_hash": price_hash.hex() if price_hash else None,
            "typed_name_sha256": sha256(typed_name).hex(),
            "client_ip_sha256": optional_sha256(client_ip),
            "user_agent_sha256": optional_sha256(agent),
            "accepted_at": at.isoformat(),
        }
    )
    written = await ctx.conn.fetchval(
        INSERT_SQL,
        acceptance_id,
        trip_id,
        proposal_id,
        traveler_id,
        link_id,
        request_id,
        source,
        doc["doc_id"],
        bytes(doc["body_sha256"]),
        price_hash,
        sha256(typed_name),
        await vault.encrypt("terms_acceptance", "client_ip_enc", acceptance_id, client_ip) if client_ip else None,
        await vault.encrypt("terms_acceptance", "user_agent_enc", acceptance_id, agent) if agent else None,
        evidence,
        sha256(evidence),
        at,
    )
    if written is None:
        raise NotFound("trip_not_found")
    ctx.audit("terms_accept", "allowed", "terms_acceptance", str(acceptance_id), reason=source)
    return acceptance_id


async def for_trip(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {
            "acceptance_id": str(r["acceptance_id"]),
            "source": r["source"],
            "doc_id": str(r["doc_id"]),
            "version_label": r["version_label"],
            "proposal_id": str(r["proposal_id"]) if r["proposal_id"] else None,
            "traveler_id": str(r["traveler_id"]) if r["traveler_id"] else None,
            "request_id": str(r["request_id"]) if r["request_id"] else None,
            "accepted_at": r["accepted_at"].isoformat(),
            "doc_sha256": r["doc_sha256"],
            "price_hash": r["price_hash"],
            "evidence_sha256": r["evidence_sha256"],
        }
        for r in await ctx.conn.fetch(TRIP_ACCEPTANCES_SQL, trip_id)
    ]
