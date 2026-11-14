"""AA-1096 L4 on PostgreSQL with the RDS role shape, after crm-064: the registry service behind /crm/ops/v1/settings and
the binding's agency read, as ``crm_app``, against the real guard triggers and RLS.

- A platform change written and approved (a single-approval key; a maker-checker key as a recorded self-approval while
  two-person approval is off), listed with its last change, history with the previous value; a stale version is 409.
- One agency's own value: written in its platform context (its settings row first, one agency audit event), read by
  ``tenant_get`` inside that agency's request transaction, invisible to every other context.
- A rejection keeps the value in force.

Platform rows are append-only (never deleted), so the values written here equal today's defaults (the other modules of
the session see the same numbers), on keys tests/integration/test_crm064_schema.py does not write at the platform scope
(it inserts version 1 rows in rolled-back transactions).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app import platform_settings as ps
from app.errors import ServiceError
from app.platform_settings import service
from app.tenancy import tenant_tx
from tests.integration.test_crm005_intake import _agency, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SUB = "00000000-0000-4000-8000-00000000aa96"
OTHER = "00000000-0000-4000-8000-00000000aa97"
REASON = "zz same value written by the integration test"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_reader() -> AsyncIterator[None]:
    ps.invalidate()
    ps.READER._snapshot = None
    yield
    ps.invalidate()
    ps.READER._snapshot = None


async def _version(pool: asyncpg.Pool, key: str) -> int:
    rows = (await service.history(pool, key, staff_sub=SUB))["history"]
    return max((r["version"] for r in rows if r["status"] in ("approved",)), default=0)


async def test_platform_changes_are_listed_with_their_history(app_pool: asyncpg.Pool) -> None:
    key = ps.INVOICE_REMINDER_EVERY_DAYS
    base = await _version(app_pool, key)
    out = await service.propose(app_pool, key, value=3, reason=REASON, staff_sub=SUB, base_version=base, apply=True)
    assert (out["status"], out["self_approved"], out["decided_by"]) == ("approved", False, f"platform:{SUB}")
    listing = await service.list_settings(app_pool)
    row = next(r for r in listing["settings"] if r["key"] == key)
    assert (row["value"], row["source"], row["last_change"]["id"]) == (3, "platform", out["id"])
    assert len(listing["settings"]) == len(ps.keys()) and listing["reader_error"] is None
    history = (await service.history(app_pool, key, staff_sub=SUB))["history"]
    assert history[0]["id"] == out["id"] and history[0]["previous_value"] == 3
    with pytest.raises(ServiceError) as err:
        await service.propose(app_pool, key, value=3, reason=REASON, staff_sub=SUB, base_version=base)
    assert err.value.code == "stale_version"


async def test_a_maker_checker_key_is_a_recorded_self_approval(app_pool: asyncpg.Pool) -> None:
    key = ps.RATE_MEMBER
    base = await _version(app_pool, key)
    out = await service.propose(app_pool, key, value=600, reason=REASON, staff_sub=SUB, base_version=base, apply=True)
    assert out["status"] == "approved" and out["self_approved"] is True
    ps.invalidate()
    await ps.READER.refresh(force=True, pool=app_pool)
    assert ps.resolve(key).source == "platform" and ps.get(key) == 600


async def test_an_agency_value_lives_in_its_own_context(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    key = ps.RATE_INTAKE_FORM
    out = await service.propose(
        app_pool, key, value=900, reason=REASON, staff_sub=SUB, base_version=0, tenant_id=tenant_id, apply=True
    )
    assert out["tenant_id"] == str(tenant_id) and out["self_approved"] is True
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        assert await ps.tenant_get(ctx.conn, key, tenant_id) == 900
    assert ps.get(key) == 200  # every other agency: the platform value
    history = await service.history(app_pool, key, tenant_id, staff_sub=SUB)
    assert [r["value"] for r in history["history"]] == [900]
    platform = (await service.history(app_pool, key, staff_sub=SUB))["history"]
    assert out["id"] not in {r["id"] for r in platform}  # never seen outside the agency
    actions = await _owner_fetch(
        admin, tenant_id, "SELECT action, object_kind, reason FROM crm.audit_event WHERE action = 'setting_change'"
    )
    assert [tuple(r) for r in actions] == [("setting_change", "platform_setting", "approved")]
    has_row = await _owner_fetch(admin, tenant_id, "SELECT count(*) AS n FROM crm.tenant_settings")
    assert has_row[0]["n"] == 1  # the agency's settings row was made first (the foreign key)
    # AA-1096 S280: the settings list names the agency with its own value (read in its own context, through RLS)
    listing = await service.list_settings(app_pool, staff_sub=SUB)
    form = next(r for r in listing["settings"] if r["key"] == key)
    mine = [r for r in form["agency_overrides"] if r["tenant_id"] == str(tenant_id)]
    assert [(r["value"], r["id"]) for r in mine] == [(900, out["id"])] and mine[0]["agency_name"]
    assert history["history"][0]["previous_value"] == 200  # the platform value it replaced, not a guess


async def test_a_rejected_change_keeps_the_value_in_force(app_pool: asyncpg.Pool) -> None:
    tenant_id, _ = await _agency(app_pool)
    key = ps.AI_BUDGET_MAX
    proposal = await service.propose(
        app_pool, key, value=5, reason=REASON, staff_sub=SUB, base_version=0, tenant_id=tenant_id
    )
    assert proposal["status"] == "proposed"
    with pytest.raises(ServiceError) as err:
        await service.propose(app_pool, key, value=6, reason=REASON, staff_sub=SUB, base_version=0, tenant_id=tenant_id)
    assert err.value.code == "open_change"
    out = await service.decide(
        app_pool, uuid.UUID(proposal["id"]), staff_sub=OTHER, approve=False, note="not now", tenant_id=tenant_id
    )
    assert out["status"] == "rejected" and out["decided_by"] == f"platform:{OTHER}"
    with pytest.raises(ServiceError) as err:
        await service.decide(app_pool, uuid.UUID(proposal["id"]), staff_sub=OTHER, approve=True)  # outside the agency
    assert err.value.code == "unknown_change"


async def test_a_range_group_is_checked_against_its_values_in_force(app_pool: asyncpg.Pool) -> None:
    key = ps.TRASH_DAYS_MAX
    base = await _version(app_pool, key)
    out = await service.propose(app_pool, key, value=30, reason=REASON, staff_sub=SUB, base_version=base, apply=True)
    assert out["status"] == "approved"  # today's value: the default (7) stays inside
    for bad_key, value in ((ps.TRASH_DAYS_MAX, 5), (ps.TRASH_DAYS_MIN, 10)):
        with pytest.raises(ServiceError) as err:
            await service.propose(
                app_pool, bad_key, value=value, reason=REASON, staff_sub=SUB, base_version=await _version(app_pool,
                bad_key))  # fmt: skip
        assert err.value.code == "range_inconsistent"
    assert (await service.history(app_pool, ps.TRASH_DAYS_MIN, staff_sub=SUB))["history"] == []  # nothing written


async def test_an_agency_value_is_refused_under_an_env_override(app_pool: asyncpg.Pool) -> None:
    tenant_id, _ = await _agency(app_pool)
    ps.configure_env({"RATE_LIMIT_INTAKE_FORM_PER_HOUR": "200"})
    try:
        with pytest.raises(ServiceError) as err:
            await service.propose(app_pool, ps.RATE_INTAKE_FORM, value=900, reason=REASON, staff_sub=SUB,
                                  base_version=0, tenant_id=tenant_id, apply=True)  # fmt: skip
        assert err.value.code == "env_override_active"
        assert (await service.history(app_pool, ps.RATE_INTAKE_FORM, tenant_id, staff_sub=SUB))["history"] == []
    finally:
        ps.configure_env({})


async def test_the_newest_approval_supersedes_the_schedules_of_its_scope(app_pool: asyncpg.Pool) -> None:
    """S280 UX fix round (the CIS rule): an approval in force cancels a schedule approved before it (which would
    otherwise replace it on its date); a future-dated approval cancels only the schedules dated at or after it."""
    tenant_id, _ = await _agency(app_pool)
    key = ps.RATE_INTAKE_FORM
    now = dt.datetime.now(dt.UTC)

    async def change(value: int, effective: dt.datetime | None, base: int = 0) -> dict:
        return await service.propose(app_pool, key, value=value, reason=REASON, staff_sub=SUB, base_version=base,
                                     effective_from=effective, tenant_id=tenant_id, apply=True)  # fmt: skip

    scheduled = await change(300, now + dt.timedelta(days=2))  # nothing in force at this scope yet: base 0
    assert scheduled["status"] == "approved" and "superseded_versions" not in scheduled
    in_force = await change(250, None)
    assert in_force["superseded_versions"] == [scheduled["version"]]
    assert in_force["cancelled_scheduled_versions"] == [scheduled["version"]]
    later = await change(400, now + dt.timedelta(days=5), in_force["version"])
    assert "superseded_versions" not in later  # the value in force stays until its date
    sooner = await change(350, now + dt.timedelta(days=3), in_force["version"])
    assert sooner["cancelled_scheduled_versions"] == [later["version"]]
    status = {r["version"]: r["status"] for r in (await service.history(app_pool, key, tenant_id, staff_sub=SUB))[
        "history"]}  # fmt: skip
    assert status == {
        scheduled["version"]: "superseded",
        in_force["version"]: "approved",
        later["version"]: "superseded",
        sooner["version"]: "approved",
    }
