"""crm-029 (AA-953 CRM-24a, an expand file) on PostgreSQL with the RDS role shape.

- ``crm.traveler_flight``: base template, FORCE RLS (an isolation probe across two agencies), crm_app DML, nothing for
  the readers; the CHECKs; one live flight per trip, traveller and direction; a flight names a traveller of the trip.
- Erasure without a replaced ``crm.erase_subject``: every key cascades, so a trip erase, a traveller erase and an
  agency erase leave no flight; a held trip keeps its flights (the hold guard).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

INSERT = (
    "INSERT INTO crm.traveler_flight (trip_id, traveler_id, direction, flight_no_enc, airport_iata, sched_local, tz, "
    "entered_by_kind) VALUES ($1, $2, $3, $4, $5, $6::text::timestamp, $7, 'traveler') RETURNING traveler_flight_id"
)


async def _flight(pool: asyncpg.Pool, ids: dict[str, Any], who: str = "lead", **kw: Any) -> uuid.UUID:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "client_link:test")
        return await conn.fetchval(
            INSERT,
            kw.get("trip", ids["trip"]),
            ids[who],
            kw.get("direction", "arrival"),
            kw.get("blob", os.urandom(40)),
            kw.get("airport", "VTE"),
            kw.get("at", "2026-12-12 06:40"),
            kw.get("tz", "Asia/Vientiane"),
        )


async def test_crm029_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-029'") == 1


async def test_the_table_is_private_to_the_agency_and_the_app(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.traveler_flight', $1)", privilege)
        for reader in READERS:
            assert not await admin.fetchval(
                "SELECT has_table_privilege($1, 'crm.traveler_flight', $2)", reader, privilege
            )
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'crm.traveler_flight'::regclass"
    )
    one, two = await _agency(app_pool), await _agency(app_pool)
    await _flight(app_pool, one)
    assert await _count(app_pool, one["tenant"], "traveler_flight") == 1
    assert await _count(app_pool, two["tenant"], "traveler_flight") == 0
    with pytest.raises(asyncpg.PostgresError):  # agency two cannot write a flight onto agency one's trip
        await _flight(app_pool, {**two, "trip": one["trip"]}, trip=one["trip"])


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"direction": "layover"}, asyncpg.CheckViolationError),
        ({"airport": "vte"}, asyncpg.CheckViolationError),
        ({"airport": "VTEX"}, asyncpg.CheckViolationError),
        ({"tz": "not a zone"}, asyncpg.CheckViolationError),
        ({"blob": b"short"}, asyncpg.CheckViolationError),
        ({"at": "1999-12-31 23:00"}, asyncpg.CheckViolationError),
    ],
)
async def test_the_checks(app_pool: asyncpg.Pool, change: dict[str, Any], error: type[Exception]) -> None:
    ids = await _agency(app_pool)
    with pytest.raises(error):
        await _flight(app_pool, ids, **change)


async def test_one_live_flight_per_direction_and_only_for_the_party(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _flight(app_pool, ids)
    await _flight(app_pool, ids, direction="departure", airport="LPQ", at="2026-12-22 21:05")
    await _flight(app_pool, ids, who="companion")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _flight(app_pool, ids)
    stranger = uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await conn.execute(
            "INSERT INTO crm.traveler (traveler_id, display_name_enc) VALUES ($1, $2)", stranger, os.urandom(40)
        )
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # not on the trip: the party key refuses it
        await _flight(app_pool, {**ids, "stranger": stranger}, who="stranger")


async def test_the_flights_leave_with_their_trip_traveller_or_agency(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _flight(app_pool, ids)
    await _flight(app_pool, ids, who="companion")
    await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert await _count(app_pool, ids["tenant"], "traveler_flight") == 0

    ids = await _agency(app_pool)
    await _flight(app_pool, ids, who="companion")
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await conn.execute("DELETE FROM crm.trip_traveler WHERE traveler_id = $1", ids["companion"])
    assert await _count(app_pool, ids["tenant"], "traveler_flight") == 0  # off the trip: the flight goes too

    ids = await _agency(app_pool)
    await _flight(app_pool, ids)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert await _count(app_pool, ids["tenant"], "traveler_flight") == 0


async def test_a_held_trip_keeps_its_flights(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    flight = await _flight(app_pool, ids)
    await _hold(app_pool, ids["tenant"], "trip", ids["trip"], "member:m")
    with pytest.raises(asyncpg.PostgresError) as err:
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], "client_link:test")
            await conn.execute("DELETE FROM crm.traveler_flight WHERE traveler_flight_id = $1", flight)
    assert err.value.sqlstate == "CRM10"
