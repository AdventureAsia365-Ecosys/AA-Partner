"""crm-052 (AA-1069, an expand file) on PostgreSQL with the RDS role shape: the agency service catalogue.

- ``crm.agency_service``: the b6 categories and the six quantity bases only; the values CHECK (name, currency,
  amounts, validity window); a service names a live own supplier or none, never the Adventure Asia row (CRM52).
- ``crm.agency_service_tier``: one tier per min_pax, max >= min, ranges of one service never overlap (CRM52).
- Who writes: a member as owner or agent; the assistant is refused (CRM52), a job / seed actor passes.
- Tenant isolation: another agency neither sees a row nor hangs a tier on a foreign service.
- Erasure: a supplier purge takes its services, the agency erase every service and tier (cascades;
  ``crm.erase_subject`` unchanged).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NEW_TABLES = ("agency_service", "agency_service_tier")


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, role: str | None = "owner",
               actor: str = "member:test") -> Any:  # fmt: skip
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        if role is not None:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _catalogue(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    t = ids["tenant"]
    await _run(pool, t, "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    ids["aa"] = await _run(pool, t, "SELECT crm.ensure_aa_supplier()", actor="seed:test", role=None)
    ids["own"] = await _run(
        pool, t,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', 'Lantern Air', 'flight') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    return ids


async def _service(pool: asyncpg.Pool, ids: dict[str, Any], **over: Any) -> uuid.UUID:
    v = {"supplier": ids["own"], "category": "flight", "name": "Hill town shuttle flight", "net": 80, "sell": 110,
         "currency": "USD", "basis": "per_person", "role": "owner", **over}  # fmt: skip
    return await _run(
        pool, ids["tenant"],
        "INSERT INTO crm.agency_service (agency_supplier_id, category, name, unit_net, unit_sell, currency, qty_basis) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING service_id",
        v["supplier"], v["category"], v["name"], v["net"], v["sell"], v["currency"], v["basis"], role=v["role"],
    )  # fmt: skip


async def _tier(pool: asyncpg.Pool, tenant: uuid.UUID, service: uuid.UUID, lo: int, hi: int | None, net: int) -> Any:
    return await _run(
        pool, tenant,
        "INSERT INTO crm.agency_service_tier (service_id, min_pax, max_pax, unit_net) VALUES ($1, $2, $3, $4) "
        "RETURNING tier_id",
        service, lo, hi, net,
    )  # fmt: skip


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_the_readers_have_nothing_on_the_new_tables(admin: asyncpg.Connection, table: str) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-052'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_the_values_checks(app_pool: asyncpg.Pool) -> None:
    ids = await _catalogue(app_pool)
    service = await _service(app_pool, ids)
    fee = await _service(app_pool, ids, supplier=None, category="fee", name="Service fee", net=None, sell=25,
                         basis="per_booking")  # fmt: skip
    assert service != fee
    for bad in ({"category": "accommodation"}, {"basis": "per_week"}, {"currency": "usd"}, {"sell": -1},
                {"net": -5}, {"name": " "}):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await _service(app_pool, ids, **bad)
    with pytest.raises(asyncpg.CheckViolationError):  # the window ends on or after it starts
        await _run(app_pool, ids["tenant"],
                   "UPDATE crm.agency_service SET valid_from = '2026-12-01', valid_to = '2026-11-01' "
                   "WHERE service_id = $1", service)  # fmt: skip


async def test_a_service_names_a_live_own_supplier(app_pool: asyncpg.Pool) -> None:
    ids = await _catalogue(app_pool)
    with pytest.raises(asyncpg.PostgresError) as err:  # never the Adventure Asia row
        await _service(app_pool, ids, supplier=ids["aa"])
    assert err.value.sqlstate == "CRM52"
    gone = await _run(
        app_pool, ids["tenant"],
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', 'Old Ferry', 'transport') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    await _run(app_pool, ids["tenant"],
               "UPDATE crm.agency_supplier SET deleted_at = now(), deleted_by = 'm', purge_after = now() "
               "WHERE agency_supplier_id = $1", gone)  # fmt: skip
    with pytest.raises(asyncpg.PostgresError) as err:  # a supplier in the trash
        await _service(app_pool, ids, supplier=gone)
    assert err.value.sqlstate == "CRM52"


async def test_only_owner_agent_or_a_job_writes_the_catalogue(app_pool: asyncpg.Pool) -> None:
    ids = await _catalogue(app_pool)
    t = ids["tenant"]
    await _service(app_pool, ids, role="agent")
    with pytest.raises(asyncpg.PostgresError) as err:
        await _service(app_pool, ids, role="assistant")
    assert err.value.sqlstate == "CRM52"
    service = await _run(
        app_pool, t,
        "INSERT INTO crm.agency_service (category, name, unit_sell, currency, qty_basis) "
        "VALUES ('visa', 'Visa letter', 30, 'USD', 'per_person') RETURNING service_id",
        role=None, actor="seed:test",
    )  # fmt: skip
    for sql in ("UPDATE crm.agency_service SET unit_sell = 0 WHERE service_id = $1",
                "DELETE FROM crm.agency_service WHERE service_id = $1"):  # fmt: skip
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql, service, role="assistant")
        assert err.value.sqlstate == "CRM52", sql
    # The assistant still reads the catalogue (the API leaves the net out for her).
    assert await _run(app_pool, t, "SELECT count(*) FROM crm.agency_service", role="assistant") == 2


async def test_tiers_never_overlap(app_pool: asyncpg.Pool) -> None:
    ids = await _catalogue(app_pool)
    t = ids["tenant"]
    service = await _service(app_pool, ids)
    await _tier(app_pool, t, service, 1, 4, 90)
    await _tier(app_pool, t, service, 5, 9, 80)
    await _tier(app_pool, t, service, 10, None, 70)
    with pytest.raises(asyncpg.PostgresError) as err:
        await _tier(app_pool, t, service, 3, 6, 85)
    assert err.value.sqlstate == "CRM52"
    with pytest.raises(asyncpg.PostgresError) as err:  # the same min_pax overlaps too (the UNIQUE is the backstop)
        await _tier(app_pool, t, service, 5, 5, 85)
    assert err.value.sqlstate == "CRM52"
    with pytest.raises(asyncpg.CheckViolationError):
        await _tier(app_pool, t, service, 20, 12, 60)
    with pytest.raises(asyncpg.PostgresError) as err:  # tiers are money fields too
        await _run(app_pool, t, "DELETE FROM crm.agency_service_tier WHERE service_id = $1", service, role="assistant")
    assert err.value.sqlstate == "CRM52"


async def test_another_agency_sees_nothing_and_cannot_hang_a_tier(app_pool: asyncpg.Pool) -> None:
    a = await _catalogue(app_pool)
    b = await _catalogue(app_pool)
    service = await _service(app_pool, a)
    await _tier(app_pool, a["tenant"], service, 1, None, 75)
    assert await _run(app_pool, b["tenant"], "SELECT count(*) FROM crm.agency_service") == 0
    assert await _run(app_pool, b["tenant"], "SELECT count(*) FROM crm.agency_service_tier") == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _tier(app_pool, b["tenant"], service, 2, None, 60)
    with pytest.raises(asyncpg.PostgresError):  # nor another agency's supplier
        await _service(app_pool, b, supplier=a["own"])


async def test_the_erases_take_the_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _catalogue(app_pool)
    t = ids["tenant"]
    service = await _service(app_pool, ids)
    await _tier(app_pool, t, service, 1, None, 70)
    await _service(app_pool, ids, supplier=None, category="fee", name="Service fee", net=None, basis="per_booking")
    await _run(app_pool, t,
               "UPDATE crm.agency_supplier SET deleted_at = now(), deleted_by = 'm', purge_after = now() "
               "WHERE agency_supplier_id = $1", ids["own"])  # fmt: skip
    await _erase(app_pool, t, "agency_supplier", ids["own"])  # the trash purge of the supplier
    assert await _count(app_pool, t, "agency_service") == 1  # the agency's own fee stays
    assert await _count(app_pool, t, "agency_service_tier") == 0
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, t, "tenant", t)
    assert await _count(app_pool, t, "agency_service") == 0
