"""The rooming list of a trip (AA-1070 item crm-17; crm-054 ``crm.room_assignment``): which room each traveller
sleeps in.

A list is ``[{room_no, room_type, travellers: [{traveler_id, bed_pref}]}]`` and is replaced whole
(:func:`replace`). It must hold:

- every traveller is on the trip and in at most one room; room numbers 1..50, one type per room number;
- a room's travellers fit its type (:data:`CAPACITY`: single 1, double 2, twin 2, triple 3, quad 4, family 6);
- when the trip's accepted proposal has a ``room_config`` (the room mix the agent priced, Fix B10), no more rooms of a
  type than it has (a type it does not list: none). Without one, only the capacities apply.

The agent edits it on the trip page; the lead traveller on the portal after the deposit (the Stage 3 lock,
:mod:`app.travel_ops.portal`). Names are shown to the agency and the lead (they are the party); the ops release and
the departure manifest carry the room number and codes only (:func:`by_traveller`).

A trip on legal hold refuses moving or removing a traveller's row (crm-054 hold guard, 409 ``legal_hold``).
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.proposals import rules
from app.tenancy import AnyContext
from app.vault import Vault

ROOM_TYPES = ("single", "double", "twin", "triple", "quad", "family")
CAPACITY = {"single": 1, "double": 2, "twin": 2, "triple": 3, "quad": 4, "family": 6}
BED_PREFS = ("double_bed", "twin_beds", "extra_bed", "cot")
MAX_ROOMS = 50
LEGAL_HOLD = "CRM10"

TRIP_SQL = "SELECT stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
PARTY_SQL = """
SELECT tt.traveler_id, tt.role, t.display_name_enc
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
ROWS_SQL = """
SELECT room_assignment_id, traveler_id, room_no, room_type, bed_pref
  FROM crm.room_assignment
 WHERE trip_id = $1 AND deleted_at IS NULL
 ORDER BY room_no, traveler_id
"""
CONFIG_SQL = """
SELECT room_config FROM crm.proposal
 WHERE trip_id = $1 AND status = 'accepted' AND deleted_at IS NULL
 ORDER BY version DESC LIMIT 1
"""
INSERT_SQL = """
INSERT INTO crm.room_assignment (trip_id, traveler_id, room_no, room_type, bed_pref) VALUES ($1, $2, $3, $4, $5)
"""
UPDATE_SQL = "UPDATE crm.room_assignment SET room_no = $2, room_type = $3, bed_pref = $4 WHERE room_assignment_id = $1"
DELETE_SQL = "DELETE FROM crm.room_assignment WHERE room_assignment_id = $1"


def config_counts(raw: Any) -> Counter[str] | None:
    """Rooms per type of the accepted proposal's ``room_config``, or None when it has none (or an unreadable one)."""
    if raw is None:
        return None
    try:
        rooms = rules.check_rooms(_json(raw))
    except ValueError:
        return None
    return Counter(r["type"] for r in rooms)


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


def check(
    rooms: Any, party: set[uuid.UUID], config: Counter[str] | None
) -> list[tuple[uuid.UUID, int, str, str | None]]:
    """The clean rows ``(traveler_id, room_no, room_type, bed_pref)`` of a list, or 422 with a code."""
    if not isinstance(rooms, list) or len(rooms) > MAX_ROOMS:
        raise Invalid("invalid_rooming", f"at most {MAX_ROOMS} rooms")
    out: list[tuple[uuid.UUID, int, str, str | None]] = []
    seen: set[uuid.UUID] = set()
    numbers: set[int] = set()
    types: Counter[str] = Counter()
    for room in rooms:
        if not isinstance(room, dict):
            raise Invalid("invalid_rooming", "a room is an object")
        number, kind, people = room.get("room_no"), room.get("room_type"), room.get("travellers")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_ROOMS:
            raise Invalid("invalid_room_no", f"1 to {MAX_ROOMS}")
        if number in numbers:
            raise Invalid("room_no_twice", "one entry per room number", room_no=number)
        numbers.add(number)
        if kind not in ROOM_TYPES:
            raise Invalid("invalid_room_type", ", ".join(ROOM_TYPES))
        if not isinstance(people, list) or not people:
            raise Invalid("room_empty", "a room has at least one traveller", room_no=number)
        if len(people) > CAPACITY[kind]:
            raise Invalid("room_over_capacity", f"a {kind} room takes {CAPACITY[kind]}", room_no=number)
        types[kind] += 1
        for person in people:
            if not isinstance(person, dict):
                raise Invalid("invalid_rooming", "a traveller is an object")
            try:
                traveler_id = uuid.UUID(str(person.get("traveler_id")))
            except ValueError:
                raise Invalid("invalid_traveler_id", "a traveller of the trip") from None
            if traveler_id not in party:
                raise Invalid("traveler_not_on_trip", "a traveller of the trip")
            if traveler_id in seen:
                raise Invalid("traveler_twice", "a traveller is in one room")
            seen.add(traveler_id)
            bed = person.get("bed_pref")
            if bed is not None and bed not in BED_PREFS:
                raise Invalid("invalid_bed_pref", ", ".join(BED_PREFS))
            out.append((traveler_id, number, kind, bed))
    if config is not None:
        over = sorted(k for k, n in types.items() if n > config.get(k, 0))
        if over:
            raise Invalid("rooms_not_in_proposal", "more rooms of a type than the accepted proposal", types=over)
    return out


async def _party(ctx: AnyContext, trip_id: uuid.UUID) -> list[Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    return list(await ctx.conn.fetch(PARTY_SQL, trip_id))


async def view(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID) -> dict[str, Any]:
    party = await _party(ctx, trip_id)
    names = {
        r["traveler_id"]: await vault.decrypt("traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"])
        for r in party
    }
    roles = {r["traveler_id"]: r["role"] for r in party}
    rows = list(await ctx.conn.fetch(ROWS_SQL, trip_id))
    rooms: dict[int, dict[str, Any]] = {}
    placed: set[uuid.UUID] = set()
    for r in rows:
        if r["traveler_id"] not in names:
            continue
        room = rooms.setdefault(r["room_no"], {"room_no": r["room_no"], "room_type": r["room_type"], "travellers": []})
        room["travellers"].append(
            {
                "traveler_id": str(r["traveler_id"]),
                "name": names[r["traveler_id"]],
                "role": roles[r["traveler_id"]],
                "bed_pref": r["bed_pref"],
            }
        )
        placed.add(r["traveler_id"])
    config = config_counts(await ctx.conn.fetchval(CONFIG_SQL, trip_id))
    return {
        "trip_id": str(trip_id),
        "rooms": [rooms[n] for n in sorted(rooms)],
        "unassigned": [
            {"traveler_id": str(r["traveler_id"]), "name": names[r["traveler_id"]], "role": r["role"]}
            for r in party
            if r["traveler_id"] not in placed
        ],
        "room_config": dict(sorted(config.items())) if config is not None else None,
        "room_types": list(ROOM_TYPES),
        "capacity": CAPACITY,
        "bed_prefs": list(BED_PREFS),
    }


async def replace(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, rooms: Any) -> dict[str, Any]:
    """Make the trip's rooming list ``rooms`` (a traveller left out has no room)."""
    party = await _party(ctx, trip_id)
    config = config_counts(await ctx.conn.fetchval(CONFIG_SQL, trip_id))
    wanted = {t: (n, k, b) for t, n, k, b in check(rooms, {r["traveler_id"] for r in party}, config)}
    current = {r["traveler_id"]: r for r in await ctx.conn.fetch(ROWS_SQL, trip_id)}
    try:
        async with ctx.conn.transaction():
            for traveler_id, row in current.items():
                if traveler_id not in wanted:
                    await ctx.conn.execute(DELETE_SQL, row["room_assignment_id"])
            for traveler_id, (number, kind, bed) in wanted.items():
                row = current.get(traveler_id)
                if row is None:
                    await ctx.conn.execute(INSERT_SQL, trip_id, traveler_id, number, kind, bed)
                elif (row["room_no"], row["room_type"], row["bed_pref"]) != (number, kind, bed):
                    await ctx.conn.execute(UPDATE_SQL, row["room_assignment_id"], number, kind, bed)
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == LEGAL_HOLD:
            raise Conflict(
                "legal_hold", "this trip is on legal hold: its rooming list cannot drop a traveller"
            ) from None
        raise
    ctx.audit("update", "allowed", "trip", str(trip_id), fields=("rooming",), object_ids=(str(trip_id),))
    return await view(ctx, vault, trip_id)


async def by_traveller(ctx: AnyContext, trip_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
    """The room of each traveller of the trip: codes and the number only (the ops release, the manifest)."""
    return {
        r["traveler_id"]: {"room_no": r["room_no"], "room_type": r["room_type"], "bed_pref": r["bed_pref"]}
        for r in await ctx.conn.fetch(ROWS_SQL, trip_id)
    }


def room_text(room: dict[str, Any] | None) -> str:
    """``"3 twin"`` / ``"3 twin, extra_bed"``: a manifest cell."""
    if not room:
        return ""
    text = f"{room['room_no']} {room['room_type']}"
    return f"{text}, {room['bed_pref']}" if room.get("bed_pref") else text
