"""Stage 3 progress of a trip (card CRM-18, AA-946; design v3 §2.7 F8 "Gate to Pre-departure: Stage 3 complete and
waiver signed"): the ``s3`` percentage on ``trip.intake_completeness`` and the per-traveller view the trip page and the
lead's portal show (progress only, never the data).

Items per traveller on the trip (``trip_traveler``):

1. **Stage 3 details**: every F1 field of :data:`DETAIL_COLUMNS` is on file (``crm.traveler``, encrypted; the
   traveller writes them in the portal, an agent may type them in too). Only presence is read, never a value.
2. **Waiver signed**, when the agency has a published waiver (``agreement_doc`` kind ``waiver``): a ``signed``
   ``signature_request`` of that version for this trip and traveller.

``s3`` = done items / all items in percent (0 for a trip without travellers). 100 opens booked -> pre_departure (the
stage engine and crm-016 ``crm.enforce_stage_transition``). The ``waiting_on`` hook ``portal_stage3`` answers
``client`` for a booked trip below 100 whose travellers have portal access (AA-943 contract, priority 60).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.intake.completeness import percent
from app.stages import TripFacts, register_waiting_on_hook
from app.tenancy import AnyContext

# The F1 columns Stage 3 needs (crm-003 / crm-006 vault columns on crm.traveler).
DETAIL_COLUMNS = (
    "given_name_enc",
    "family_name_enc",
    "dob_enc",
    "nationality_enc",
    "passport_no_enc",
    "passport_expiry_enc",
    "passport_country_enc",
    "emergency_contact_enc",
)
DETAIL_FIELDS = tuple(c.removesuffix("_enc") for c in DETAIL_COLUMNS)
HOOK_PRIORITY = 60

WAIVER_SQL = """
SELECT doc_id, version_label, body_sha256 FROM crm.agreement_doc
 WHERE kind = 'waiver' AND status = 'published' AND deleted_at IS NULL
"""
PARTY_SQL = f"""
SELECT tt.traveler_id, tt.role,
       {" AND ".join(f"t.{c} IS NOT NULL" for c in DETAIL_COLUMNS)} AS details_done,
       ARRAY[{", ".join(f"t.{c} IS NOT NULL" for c in DETAIL_COLUMNS)}] AS details_have,
       a.access_id, a.status AS access_status, a.last_sign_in_at, a.stage3_submitted_at,
       EXISTS (SELECT 1 FROM crm.signature_request s
                WHERE s.trip_id = tt.trip_id AND s.traveler_id = tt.traveler_id AND s.doc_id = $2
                  AND s.status = 'signed' AND s.deleted_at IS NULL) AS waiver_signed
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
  LEFT JOIN crm.traveler_access a
         ON a.trip_id = tt.trip_id AND a.traveler_id = tt.traveler_id AND a.status <> 'revoked'
        AND a.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""  # noqa: S608 — column names from the constant above
STORE_SQL = """
UPDATE crm.trip SET intake_completeness = jsonb_set(intake_completeness, '{s3}', to_jsonb($2::int))
 WHERE trip_id = $1 AND (intake_completeness ->> 's3')::int IS DISTINCT FROM $2
"""
CLIENT_TURN_SQL = """
SELECT coalesce((t.intake_completeness ->> 's3')::int, 0) < 100
       AND EXISTS (SELECT 1 FROM crm.traveler_access a
                    WHERE a.trip_id = t.trip_id AND a.status <> 'revoked' AND a.deleted_at IS NULL)
  FROM crm.trip t WHERE t.trip_id = $1
"""


@dataclass(frozen=True)
class TravellerProgress:
    traveler_id: uuid.UUID
    role: str
    details_done: bool
    missing: tuple[str, ...]
    waiver_signed: bool | None  # None = the agency has no published waiver
    access_id: uuid.UUID | None
    access_status: str | None
    last_sign_in_at: Any
    stage3_submitted_at: Any

    def view(self) -> dict[str, Any]:
        return {
            "traveler_id": str(self.traveler_id),
            "role": self.role,
            "details_done": self.details_done,
            "missing": list(self.missing),
            "waiver_signed": self.waiver_signed,
            "portal": None
            if self.access_id is None
            else {
                "access_id": str(self.access_id),
                "status": self.access_status,
                "last_sign_in_at": _iso(self.last_sign_in_at),
                "stage3_submitted_at": _iso(self.stage3_submitted_at),
            },
        }


@dataclass(frozen=True)
class Progress:
    s3: int
    waiver_doc_id: uuid.UUID | None
    travellers: tuple[TravellerProgress, ...]

    def view(self) -> dict[str, Any]:
        return {
            "s3": self.s3,
            "waiver_required": self.waiver_doc_id is not None,
            "travellers": [t.view() for t in self.travellers],
        }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def compute(ctx: AnyContext, trip_id: uuid.UUID) -> Progress:
    waiver = await ctx.conn.fetchrow(WAIVER_SQL)
    waiver_id = waiver["doc_id"] if waiver is not None else None
    rows = await ctx.conn.fetch(PARTY_SQL, trip_id, waiver_id)
    travellers = []
    done = 0
    for r in rows:
        have = list(r["details_have"] or [])
        missing = tuple(f for f, ok in zip(DETAIL_FIELDS, have, strict=False) if not ok)
        signed = bool(r["waiver_signed"]) if waiver_id is not None else None
        done += int(bool(r["details_done"])) + int(bool(signed))
        travellers.append(
            TravellerProgress(
                traveler_id=r["traveler_id"],
                role=r["role"],
                details_done=bool(r["details_done"]),
                missing=missing,
                waiver_signed=signed,
                access_id=r["access_id"],
                access_status=r["access_status"],
                last_sign_in_at=r["last_sign_in_at"],
                stage3_submitted_at=r["stage3_submitted_at"],
            )
        )
    items = len(rows) * (2 if waiver_id is not None else 1)
    s3 = percent(done, items) if items else 0
    return Progress(s3=s3, waiver_doc_id=waiver_id, travellers=tuple(travellers))


async def refresh(ctx: AnyContext, trip_id: uuid.UUID) -> Progress:
    """Compute and store ``s3`` on the trip."""
    done = await compute(ctx, trip_id)
    await ctx.conn.execute(STORE_SQL, trip_id, done.s3)
    return done


async def _stage3_hook(ctx: AnyContext, facts: TripFacts) -> str | None:
    """``waiting_on`` for a booked trip: the client while they have portal access and Stage 3 or a waiver is missing
    (stored s3 < 100). Without a portal invite the client has nothing to do yet: no opinion (the base rule)."""
    if facts.stage != "booked":
        return None
    return "client" if await ctx.conn.fetchval(CLIENT_TURN_SQL, facts.trip_id) is True else None


register_waiting_on_hook(HOOK_PRIORITY, "portal_stage3", _stage3_hook)
