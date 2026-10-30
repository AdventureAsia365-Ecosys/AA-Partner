"""The signing log (AA-955, card CRM-25; ``crm.signature_event``, append-only).

- ``code_sent``: a signing code went out for this traveller's envelope;
- ``code_refused``: a signing attempt with an expired, wrong or missing code, or after the last attempt (card R: "an
  expired code is refused and logged"); the refusal answer still goes back to the signer;
- ``signed``: the sealed envelope (docs DEK), its SHA-256, the evidence record and the seal, the client address and
  user agent (vault DEK).

No foreign key on the request / trip / traveller / access: DM-7 claim-period exception (the row outlives their erasure
until ``keep_until`` = trip end + 3 years, est.).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.esign.seal import Seal
from app.tenancy import AnyContext
from app.vault import Vault

REFUSAL_REASONS = ("code_expired", "code_invalid", "code_missing", "attempts_exhausted")
KEEP_UNTIL_SQL = """
(SELECT greatest((coalesce(t.end_date, t.start_date, current_date) + interval '3 years')::date, current_date)
   FROM crm.trip t WHERE t.trip_id = $3)
"""
LOG_SQL = f"""
INSERT INTO crm.signature_event (event_id, kind, trip_id, traveler_id, signer_traveler_id, access_id, reason,
                                 keep_until)
VALUES ($1, $2, $3, $4, $5, $6, $7, {KEEP_UNTIL_SQL})
"""  # noqa: S608 — a constant
SIGNED_SQL = f"""
INSERT INTO crm.signature_event (event_id, kind, trip_id, traveler_id, signer_traveler_id, access_id, request_id,
                                 evidence_record, evidence_sha256, envelope_enc, envelope_sha256, seal_alg,
                                 seal_key_ref, seal_value, client_ip_enc, user_agent_enc, occurred_at, keep_until)
VALUES ($1, 'signed', $3, $4, $5, $6, $2, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, {KEEP_UNTIL_SQL})
"""  # noqa: S608 — a constant
ENVELOPE_SQL = """
SELECT event_id, request_id, trip_id, traveler_id, envelope_enc, envelope_sha256, seal_alg, seal_key_ref, seal_value,
       evidence_sha256, occurred_at
  FROM crm.signature_event WHERE request_id = $1 AND kind = 'signed'
 ORDER BY occurred_at DESC LIMIT 1
"""
TRIP_EVENTS_SQL = """
SELECT traveler_id, kind, reason, occurred_at FROM crm.signature_event
 WHERE trip_id = $1 ORDER BY occurred_at DESC, event_id LIMIT 100
"""


async def log(
    ctx: AnyContext,
    kind: str,
    access: Any,
    *,
    traveler_id: uuid.UUID,
    reason: str | None = None,
) -> None:
    """A ``code_sent`` or ``code_refused`` row. ``access`` is the signer's portal access row (access_id, trip_id,
    traveler_id); ``traveler_id`` the traveller the envelope is for (a minor when a guardian signs)."""
    if kind == "code_refused" and reason not in REFUSAL_REASONS:
        raise ValueError(f"unknown refusal reason {reason!r}")
    signer = access["traveler_id"] if access["traveler_id"] != traveler_id else None
    await ctx.conn.execute(
        LOG_SQL,
        uuid.uuid4(),
        kind,
        access["trip_id"],
        traveler_id,
        signer,
        access["access_id"],
        reason if kind == "code_refused" else None,
    )


async def signed(
    ctx: AnyContext,
    vault: Vault,
    *,
    request_id: uuid.UUID,
    access: Any,
    traveler_id: uuid.UUID,
    evidence: bytes,
    evidence_sha256: bytes,
    envelope: bytes,
    envelope_sha256: bytes,
    seal: Seal,
    client_ip: str | None,
    user_agent: str | None,
    at: Any,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    signer = access["traveler_id"] if access["traveler_id"] != traveler_id else None
    await ctx.conn.execute(
        SIGNED_SQL,
        event_id,
        request_id,
        access["trip_id"],
        traveler_id,
        signer,
        access["access_id"],
        evidence,
        evidence_sha256,
        await vault.encrypt("signature_event", "envelope", event_id, envelope, purpose="docs"),
        envelope_sha256,
        seal.alg,
        seal.key_ref,
        seal.value,
        await vault.encrypt("signature_event", "client_ip_enc", event_id, client_ip) if client_ip else None,
        await vault.encrypt("signature_event", "user_agent_enc", event_id, user_agent) if user_agent else None,
        at,
    )
    return event_id


async def envelope_row(ctx: AnyContext, request_id: uuid.UUID) -> Any:
    return await ctx.conn.fetchrow(ENVELOPE_SQL, request_id)


async def for_trip(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {
            "traveler_id": str(r["traveler_id"]),
            "kind": r["kind"],
            "reason": r["reason"],
            "occurred_at": r["occurred_at"].isoformat(),
        }
        for r in await ctx.conn.fetch(TRIP_EVENTS_SQL, trip_id)
    ]
