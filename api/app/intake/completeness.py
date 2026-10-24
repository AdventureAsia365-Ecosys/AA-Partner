"""Stage 1 / 2 / 3 completeness of a trip (design v3 §2.7 F2, the Traveller info panel; card CRM-15).

Completeness is a percentage per stage plus the field codes still missing, computed from what the agency holds:

- **s1** (the inquiry): the trip's own answers (destinations, when, nights, party type, budget, trip style) and a way
  to reach the lead (e-mail or phone on file, never their value);
- **s2** (the planning form): the lead's F2 profile (bands and codes only, reached through the encrypted
  ``traveler.profile_token_enc``). Altitude and trek experience count only for an adventure trip (R31: the form shows
  them only then), so a relaxed trip is not held below 80 % by questions it never asks;
- **s3** (booking details) is CRM-25's: kept as stored.

:func:`refresh` writes ``crm.trip.intake_completeness`` (the Proposal gate reads ``s2``, crm-012) and the profile's
``stage2_completeness``, and keeps the "Get planning info" task in step: open while a Qualifying trip is below 80 %,
done once it reaches it (design §2.7 F2 step 2).

Every function takes an open context (``TenantContext`` or ``SystemContext``) and never opens one.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.tenancy import AnyContext
from app.vault import Vault

STAGE2_GATE = 80
PLANNING_TASK = "get_planning_info"

S1_FIELDS = ("destinations", "when", "nights", "party_type", "budget_band", "trip_style", "contact")
S2_FIELDS = (
    "fitness_band",
    "accommodation_tier",
    "group_pref",
    "interests",
    "occasion",
    "budget_flex",
    "decision_maker",
    "booking_timeline",
    "competing_quotes",
)
S2_ADVENTURE_FIELDS = ("trek_experience", "altitude_ok")

TRIP_SQL = """
SELECT t.stage, t.destinations, t.month, t.start_date, t.nights, t.party_type, t.budget_band, t.trip_style,
       t.intake_completeness
  FROM crm.trip t WHERE t.trip_id = $1 AND t.deleted_at IS NULL
"""
LEAD_SQL = """
SELECT tr.traveler_id, tr.profile_token_enc, (tr.email_enc IS NOT NULL OR tr.phone_enc IS NOT NULL) AS reachable
  FROM crm.trip_traveler tt JOIN crm.traveler tr ON tr.traveler_id = tt.traveler_id
 WHERE tt.trip_id = $1 AND tt.role = 'lead' AND tt.deleted_at IS NULL AND tr.deleted_at IS NULL
 LIMIT 1
"""
PROFILE_SQL = """
SELECT fitness_band, trek_experience, altitude_ok, accommodation_tier, group_pref, interests, occasion, budget_flex,
       decision_maker, booking_timeline, competing_quotes
  FROM crm.traveler_profile WHERE traveler_token = $1 AND deleted_at IS NULL
"""
STORE_SQL = """
UPDATE crm.trip SET intake_completeness = $2::jsonb
 WHERE trip_id = $1 AND intake_completeness IS DISTINCT FROM $2::jsonb
"""
PROFILE_STORE_SQL = "UPDATE crm.traveler_profile SET stage2_completeness = $2 WHERE traveler_token = $1"
OPEN_TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, due_anchor, due_offset_days, due_at)
VALUES ($1, $2, 'stage_entered', 2, now() + interval '2 days')
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
CLOSE_TASK_SQL = """
UPDATE crm.task SET status = 'done', done_at = now(), done_by = $3
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL
"""


@dataclass(frozen=True)
class Completeness:
    s1: int
    s2: int
    s3: int
    missing_s1: tuple[str, ...]
    missing_s2: tuple[str, ...]

    def stored(self) -> dict[str, int]:
        return {"s1": self.s1, "s2": self.s2, "s3": self.s3}

    def view(self) -> dict[str, Any]:
        return {
            "s1": self.s1,
            "s2": self.s2,
            "s3": self.s3,
            "missing_s1": list(self.missing_s1),
            "missing_s2": list(self.missing_s2),
            "stage2_gate": STAGE2_GATE,
        }


def percent(filled: int, total: int) -> int:
    return 100 if total == 0 else (100 * filled) // total


def s1_missing(trip: Any, reachable: bool) -> tuple[str, ...]:
    have = {
        "destinations": bool(trip["destinations"]) and list(trip["destinations"]) != ["not_sure"],
        "when": trip["month"] is not None or trip["start_date"] is not None,
        "nights": trip["nights"] is not None,
        "party_type": trip["party_type"] is not None,
        "budget_band": trip["budget_band"] not in (None, "not_sure"),
        "trip_style": trip["trip_style"] is not None,
        "contact": reachable,
    }
    return tuple(f for f in S1_FIELDS if not have[f])


def s2_fields(trip_style: str | None) -> tuple[str, ...]:
    """The Stage 2 questions that count for this trip (R31: the adventure branch only for an adventure trip)."""
    return S2_FIELDS + (S2_ADVENTURE_FIELDS if trip_style == "adventure" else ())


def s2_missing(profile: Any | None, trip_style: str | None) -> tuple[str, ...]:
    fields = s2_fields(trip_style)
    if profile is None:
        return fields
    return tuple(f for f in fields if profile[f] is None or (f == "interests" and not profile[f]))


def _stored(raw: Any) -> dict[str, int]:
    value = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    return {k: int(value.get(k, 0)) for k in ("s1", "s2", "s3")}


async def lead_token(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """The lead traveller and their F2 profile token (None, None without a lead)."""
    lead = await ctx.conn.fetchrow(LEAD_SQL, trip_id)
    if lead is None:
        return None, None
    token = None
    if lead["profile_token_enc"] is not None:
        token = uuid.UUID(
            await vault.decrypt("traveler", "profile_token_enc", lead["traveler_id"], lead["profile_token_enc"])
        )
    return lead["traveler_id"], token


async def compute(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID) -> Completeness | None:
    """The trip's completeness now (None for an unknown trip)."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        return None
    lead = await ctx.conn.fetchrow(LEAD_SQL, trip_id)
    profile = None
    if lead is not None and lead["profile_token_enc"] is not None:
        token = uuid.UUID(
            await vault.decrypt("traveler", "profile_token_enc", lead["traveler_id"], lead["profile_token_enc"])
        )
        profile = await ctx.conn.fetchrow(PROFILE_SQL, token)
    missing1 = s1_missing(trip, bool(lead and lead["reachable"]))
    missing2 = s2_missing(profile, trip["trip_style"])
    total2 = len(s2_fields(trip["trip_style"]))
    return Completeness(
        s1=percent(len(S1_FIELDS) - len(missing1), len(S1_FIELDS)),
        s2=percent(total2 - len(missing2), total2),
        s3=_stored(trip["intake_completeness"])["s3"],
        missing_s1=missing1,
        missing_s2=missing2,
    )


async def refresh(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, *, actor: str) -> Completeness | None:
    """Compute, store on the trip and the lead's profile, and keep the planning task in step. ``actor`` closes the
    task (``member:<id>`` or ``job:<kind>``)."""
    done = await compute(ctx, vault, trip_id)
    if done is None:
        return None
    await ctx.conn.execute(STORE_SQL, trip_id, json.dumps(done.stored()))
    _lead, token = await lead_token(ctx, vault, trip_id)
    if token is not None:
        await ctx.conn.execute(PROFILE_STORE_SQL, token, done.s2)
    stage = await ctx.conn.fetchval("SELECT stage FROM crm.trip WHERE trip_id = $1", trip_id)
    await planning_task(ctx, trip_id, stage, done.s2, actor=actor)
    return done


async def planning_task(ctx: AnyContext, trip_id: uuid.UUID, stage: str | None, s2: int, *, actor: str) -> None:
    """Design §2.7 F2: a Qualifying trip below 80 % has an open "Get planning info" task; at 80 % it is done."""
    if s2 >= STAGE2_GATE:
        await ctx.conn.execute(CLOSE_TASK_SQL, trip_id, PLANNING_TASK, actor)
    elif stage == "qualifying":
        await ctx.conn.execute(OPEN_TASK_SQL, trip_id, PLANNING_TASK)
