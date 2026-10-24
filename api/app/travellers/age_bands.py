"""The age band at departure (R32; design v3 §3.3 group C, lane §2.4, §5.7 "volume control").

``crm.trip_party.age_band_at_departure`` (F2) holds the traveller's band on the trip's start date, so AI and insight
views get an age without the DOB (F1). It is recomputed **only** when one of its two inputs changes:

- the traveller's DOB (:func:`recompute_for_traveler`, called by the F1 edit when ``dob`` changed), or
- the trip's start date (:func:`recompute_for_trip`, called by the trip edit when ``start_date`` changed), or
- the traveller joins a trip (:func:`recompute_party_member`).

Any other edit leaves the bands alone (card CRM-09 E test: no F5 noise). A recompute reads DOBs (F1) as a bulk step and
writes **one** ``audit_event`` per run (action ``age_band_recompute``, field ``dob``, the trips' ids), not one per
traveller. A traveller without a DOB, or a trip without a start date, has no band (``NULL``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from app.tenancy import TenantContext
from app.vault import Vault

# (band code, lowest age in it), highest first. Mirrors crm-006 trip_party_age_band_list_chk.
BANDS: tuple[tuple[str, int], ...] = (
    ("75_plus", 75),
    ("65_74", 65),
    ("55_64", 55),
    ("45_54", 45),
    ("35_44", 35),
    ("25_34", 25),
    ("18_24", 18),
    ("12_17", 12),
    ("2_11", 2),
    ("under_2", 0),
)
BAND_CODES = tuple(code for code, _ in reversed(BANDS))

PARTY_OF_TRAVELER_SQL = """
SELECT tp.trip_party_id, tp.trip_id, tp.age_band_at_departure, t.start_date
  FROM crm.trip_party tp JOIN crm.trip t ON t.trip_id = tp.trip_id
 WHERE tp.traveler_token = $1 AND tp.deleted_at IS NULL AND t.deleted_at IS NULL
"""
TRIP_TRAVELERS_SQL = """
SELECT tt.traveler_id, tr.dob_enc, tr.profile_token_enc
  FROM crm.trip_traveler tt JOIN crm.traveler tr ON tr.traveler_id = tt.traveler_id AND tr.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
"""
TRIP_PARTY_SQL = """
SELECT trip_party_id, traveler_token, age_band_at_departure FROM crm.trip_party
 WHERE trip_id = $1 AND deleted_at IS NULL
"""
SET_BAND_SQL = "UPDATE crm.trip_party SET age_band_at_departure = $2 WHERE trip_party_id = $1"
DOB_SQL = "SELECT dob_enc, profile_token_enc FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"


def age_on(dob: dt.date, day: dt.date) -> int:
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def age_band(dob: dt.date | None, departure: dt.date | None) -> str | None:
    """The band on ``departure``, or ``None`` without both dates (or a DOB after the departure)."""
    if dob is None or departure is None or dob > departure:
        return None
    age = age_on(dob, departure)
    return next(code for code, low in BANDS if age >= low)


async def recompute_for_traveler(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID, dob: dt.date | None) -> int:
    """After the traveller's DOB changed (``dob`` = the new value): every trip party row of the traveller. Returns the
    number of rows whose band changed; one audit event when any party row exists."""
    row = await ctx.conn.fetchrow(DOB_SQL, traveler_id)
    if row is None or row["profile_token_enc"] is None:
        return 0
    token = uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, row["profile_token_enc"]))
    parties = await ctx.conn.fetch(PARTY_OF_TRAVELER_SQL, token)
    changed = await _apply(ctx, [(p, age_band(dob, p["start_date"])) for p in parties])
    if parties:
        ctx.audit(
            "age_band_recompute",
            "allowed",
            "traveler",
            str(traveler_id),
            fields=("dob",),
            reason="dob_changed",
            object_ids=tuple(sorted({str(p["trip_id"]) for p in parties})),
        )
    return changed


async def recompute_for_trip(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID, start: dt.date | None) -> int:
    """After the trip's start date changed (``start`` = the new value): every party member of the trip, one event."""
    party = {r["traveler_token"]: r for r in await ctx.conn.fetch(TRIP_PARTY_SQL, trip_id)}
    pairs: list[tuple[Any, str | None]] = []
    for r in await ctx.conn.fetch(TRIP_TRAVELERS_SQL, trip_id):
        if r["profile_token_enc"] is None:
            continue
        token = uuid.UUID(
            await vault.decrypt("traveler", "profile_token_enc", r["traveler_id"], r["profile_token_enc"])
        )
        twin = party.get(token)  # the F2 twin of this F1 party row
        if twin is not None:
            pairs.append((twin, age_band(await _dob(vault, r["traveler_id"], r["dob_enc"]), start)))
    changed = await _apply(ctx, pairs)
    if party:
        ctx.audit(
            "age_band_recompute",
            "allowed",
            "trip",
            str(trip_id),
            fields=("dob",),
            reason="start_date_changed",
        )
    return changed


async def recompute_party_member(
    ctx: TenantContext,
    vault: Vault,
    trip_party_id: uuid.UUID,
    traveler_id: uuid.UUID,
    dob_enc: bytes | None,
    start: dt.date | None,
) -> str | None:
    """A traveller just joined a trip: the band of that one new party row (part of the add's own audit event)."""
    band = age_band(await _dob(vault, traveler_id, dob_enc), start)
    if band is not None:
        await ctx.conn.execute(SET_BAND_SQL, trip_party_id, band)
    return band


async def _dob(vault: Vault, traveler_id: uuid.UUID, blob: bytes | None) -> dt.date | None:
    if blob is None:
        return None
    return dt.date.fromisoformat(await vault.decrypt("traveler", "dob_enc", traveler_id, blob))


async def _apply(ctx: TenantContext, pairs: list[tuple[Any, str | None]]) -> int:
    changed = 0
    for row, band in pairs:
        if row["age_band_at_departure"] != band:
            await ctx.conn.execute(SET_BAND_SQL, row["trip_party_id"], band)
            changed += 1
    return changed
