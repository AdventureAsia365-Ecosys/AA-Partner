"""AA-1096 L4 (crm-064): the CRM binding of the settings reader (``app.platform_settings``) and the registry service
behind ``/crm/ops/v1/settings`` (``app.platform_settings.service``), on scripted connections. The SQL itself (RLS, the
guard triggers) is covered by tests/integration/test_crm064_schema.py and test_aa1096_settings_db.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import platform_settings as ps
from app.errors import ServiceError
from app.observability import ops_alerts
from app.platform_settings import service
from app.ratelimit import FixedWindowLimiter
from app.settings import Settings, load_settings
from tests.unit.fakes import ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000001a1")
VID = uuid.UUID("00000000-0000-4000-8000-0000000001b2")
NOW = dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.UTC)
REASON = "zz a reason long enough for the check"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ps.configure_env({})
    ps.invalidate()
    ps.READER._snapshot = None
    ops_alerts.PENDING.drain()
    yield
    ps.configure_env({})
    ps.invalidate()
    ps.READER._snapshot = None
    ops_alerts.PENDING.drain()


def _row(**over: Any) -> dict[str, Any]:
    value = over.pop("value", 4)
    row = {
        "id": VID,
        "key": "crm.payments.invoice_reminder_max",
        "tenant_id": None,
        "value_text": json.dumps(value),
        "version": 1,
        "status": "approved",
        "effective_from": NOW,
        "proposed_by": "platform:zz-staff",
        "proposed_at": NOW,
        "reason": REASON,
        "evidence_ref": None,
        "self_approved": False,
        "decided_by": "platform:zz-staff",
        "decided_at": NOW,
        "decision_note": None,
    }
    row.update(over)
    return row


# ── the binding ──────────────────────────────────────────────────────────────────────────────────


def test_every_key_constant_is_a_compiled_key_and_defaults_are_todays_values() -> None:
    names = [
        v
        for k, v in vars(ps).items()
        if k.isupper() and isinstance(v, str) and v.count(".") >= 2 and v.startswith("crm.")
    ]
    assert set(names) <= set(ps.keys()) and len(names) == len(ps.keys())
    assert ps.get(ps.RATE_PUBLIC) == 60  # Terraform's value, not the old code default 120
    assert (ps.get(ps.RATE_INTAKE), ps.get(ps.RATE_INTAKE_FORM), ps.get(ps.RATE_MEMBER), ps.get(ps.RATE_AI)) == (
        5,
        200,
        600,
        6,
    )
    assert (ps.get(ps.AI_BUDGET_MAX), ps.get(ps.AI_DRAFT_MAX_TOKENS)) == (2.0, 800)
    assert (ps.get(ps.INVOICE_REMINDER_EVERY_DAYS), ps.get(ps.INVOICE_REMINDER_MAX)) == (3, 5)
    assert ps.get(ps.LIFECYCLE_DRY_RUN) is True and ps.get(ps.PARTNER_CLEANUP_DRY_RUN) is False
    assert ps.resolve(ps.RATE_PUBLIC).source == "default"


def test_an_env_override_wins_and_a_bad_one_is_ignored() -> None:
    ps.configure_env({"RATE_LIMIT_PUBLIC_PER_MINUTE": "70", "RATE_LIMIT_AI_PER_MINUTE": "lots"})
    res = ps.resolve(ps.RATE_PUBLIC)
    assert (res.value, res.source, res.env_name) == (70, "env", "RATE_LIMIT_PUBLIC_PER_MINUTE")
    bad = ps.resolve(ps.RATE_AI)
    assert bad.value == 6 and bad.source == "default" and bad.env_ignored


def test_settings_env_names_the_overrides_the_environment_set_or_a_test_changed() -> None:
    loaded = load_settings(
        {"ENVIRONMENT": "test", "RATE_LIMIT_PUBLIC_PER_MINUTE": "60", "RATE_LIMIT_INTAKE_PER_MINUTE": "5"}
    )
    assert loaded.settings_env() == {"RATE_LIMIT_PUBLIC_PER_MINUTE": "60", "RATE_LIMIT_INTAKE_PER_MINUTE": "5"}
    assert Settings().settings_env() == {}
    assert Settings(rate_limit_member_per_minute=3).settings_env() == {"RATE_LIMIT_MEMBER_PER_MINUTE": "3"}


@pytest.mark.asyncio
async def test_an_agency_value_is_read_in_its_context_and_cached() -> None:
    conn = ScriptedConn().on(
        "FROM crm.setting_value v WHERE v.tenant_id = $1",
        [
            {"key": ps.RATE_INTAKE_FORM, "value_text": "900", "version": 2},
            {"key": "crm.zz.newer_key", "value_text": "1", "version": 1},
        ],
    )
    assert await ps.tenant_get(conn, ps.RATE_INTAKE_FORM, TENANT) == 900
    res = ps.resolve(ps.RATE_INTAKE_FORM, TENANT)
    assert (res.source, res.version) == ("tenant", 2)
    assert ps.get(ps.RATE_INTAKE_FORM) == 200  # the platform value for everyone else
    assert await ps.tenant_get(conn, ps.RATE_INTAKE_FORM, TENANT) == 900
    assert len(conn.sql("WHERE v.tenant_id = $1")) == 1  # cached for one window
    assert await ps.tenant_get(conn, ps.RATE_AI, TENANT) == 6  # a platform-only key never reads the agency
    ps.forget_tenant(TENANT)
    assert ps.resolve(ps.RATE_INTAKE_FORM, TENANT).source == "default"


@pytest.mark.asyncio
async def test_an_env_override_beats_an_agency_value() -> None:
    conn = ScriptedConn().on(
        "WHERE v.tenant_id = $1", [{"key": ps.RATE_INTAKE_FORM, "value_text": "900", "version": 2}]
    )
    ps.configure_env({"RATE_LIMIT_INTAKE_FORM_PER_HOUR": "50"})
    assert await ps.tenant_get(conn, ps.RATE_INTAKE_FORM, TENANT) == 50


@pytest.mark.asyncio
async def test_a_broken_agency_value_is_refused_and_raises_an_ops_alert() -> None:
    conn = ScriptedConn().on(
        "WHERE v.tenant_id = $1",
        [
            {"key": ps.RATE_INTAKE_FORM, "value_text": "-5", "version": 3},
            {"key": ps.AI_BUDGET_MAX, "value_text": "not json", "version": 1},
            {"key": ps.RATE_PUBLIC, "value_text": "70", "version": 1},  # a platform-only key with an agency row
        ],
    )
    assert await ps.tenant_refresh(conn, TENANT) is True
    assert ps.get(ps.RATE_INTAKE_FORM, TENANT) == 200 and ps.get(ps.AI_BUDGET_MAX, TENANT) == 2.0
    checks = [f.check for f in ops_alerts.PENDING.drain()]
    assert checks.count("setting_value_refused") == 3


@pytest.mark.asyncio
async def test_a_failed_agency_read_keeps_the_last_good_rows() -> None:
    good = ScriptedConn().on(
        "WHERE v.tenant_id = $1", [{"key": ps.RATE_INTAKE_FORM, "value_text": "900", "version": 2}]
    )
    await ps.tenant_refresh(good, TENANT)
    broken = ScriptedConn().on("WHERE v.tenant_id = $1", asyncpg.UndefinedTableError("no table"))
    assert await ps.tenant_refresh(broken, TENANT, force=True) is False
    assert ps.get(ps.RATE_INTAKE_FORM, TENANT) == 900
    assert await ps.tenant_refresh(broken, uuid.uuid4(), force=True) is False  # no rows yet: the platform value


@pytest.mark.asyncio
async def test_the_agency_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ps, "TENANT_CACHE_MAX", 2)
    conn = ScriptedConn()
    for _ in range(3):
        await ps.tenant_refresh(conn, uuid.uuid4())
    assert len(ps._TENANT) == 2


def test_limits_and_dry_run_switches_read_now() -> None:
    limiter = FixedWindowLimiter(ps.limit(ps.RATE_INTAKE))
    assert all(limiter.allow("a") for _ in range(5)) and not limiter.allow("a")
    ps.configure_env({"RATE_LIMIT_INTAKE_PER_MINUTE": "0"})  # 0 = off, read at the next call
    assert limiter.allow("a") and not limiter.exhausted("a")
    assert ps.flag(ps.dry_run(True, ps.PARTNER_CLEANUP_DRY_RUN)) is True  # the Terraform floor wins
    assert ps.flag(ps.dry_run(False, ps.PARTNER_CLEANUP_DRY_RUN)) is False  # both keys off: runs
    assert ps.flag(ps.dry_run(False, ps.LIFECYCLE_DRY_RUN)) is True  # the admin switch is on (seed)
    assert ps.flag(False) is False and ps.flag(lambda: 1) is True


@pytest.mark.asyncio
async def test_the_refresher_waits_for_the_pool_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ps.start_refresher(lambda: None) is None  # ENVIRONMENT=test: tests read the defaults
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setattr(ps.READER, "max_age_s", 0.01)
    calls = {"n": 0}
    conn = ScriptedConn().on(
        "FROM crm.setting_value v",
        [{"key": ps.RATE_PUBLIC, "tenant_id": None, "value_text": "75", "version": 4, "effective_from": NOW}],
    )

    async def get_pool() -> ScriptedConn:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("not yet")
        return conn

    task = ps.start_refresher(get_pool)
    assert task is not None
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ps.READER._snapshot:
            break
    await ps.stop_refresher(task)
    await ps.stop_refresher(None)
    assert calls["n"] >= 2 and ps.get(ps.RATE_PUBLIC) == 75 and ps.resolve(ps.RATE_PUBLIC).source == "platform"
    ps.install(conn)
    assert await ps.aget(ps.RATE_PUBLIC) == 75


# ── the registry service ─────────────────────────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, conn: ScriptedConn) -> None:
        self.conn = conn
        self.events: list[tuple[Any, ...]] = []

    def audit(self, *args: Any, **kw: Any) -> None:
        self.events.append((*args, kw.get("reason")))


@pytest.fixture
def agency(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_platform_tx(pool: Any, tenant_id: uuid.UUID, *, actor: str, statuses: Any) -> Any:
        ctx = _Ctx(pool)
        seen.update(tenant_id=tenant_id, actor=actor, ctx=ctx)
        yield ctx

    monkeypatch.setattr(service, "platform_tx", fake_platform_tx)
    return seen


@pytest.mark.asyncio
async def test_list_settings_shows_values_sources_and_open_changes() -> None:
    ps.configure_env({"CRM_LLM_MAX_TOKENS": "900"})
    conn = ScriptedConn()
    conn.on("v.status IN ('approved', 'rejected', 'superseded')", [_row()])
    conn.on("v.tenant_id IS NULL AND v.status = 'proposed'", [_row(key=ps.RATE_AI, status="proposed", value=7)])
    out = await service.list_settings(conn)
    by_key = {r["key"]: r for r in out["settings"]}
    assert out["app"] == "crm" and len(by_key) == len(ps.keys()) and "rate_limit" in out["areas"]
    assert by_key[ps.AI_DRAFT_MAX_TOKENS]["overridden_by_env"] and by_key[ps.AI_DRAFT_MAX_TOKENS]["value"] == 900
    assert by_key[ps.INVOICE_REMINDER_MAX]["last_change"]["value"] == 4
    assert by_key[ps.RATE_AI]["pending"][0]["value"] == 7 and out["two_person_approval"] is False
    only = await service.list_settings(conn, area="payments")
    assert {r["area"] for r in only["settings"]} == {"payments"}


@pytest.mark.asyncio
async def test_routes_answer_503_before_crm_064() -> None:
    conn = ScriptedConn().on("FROM crm.setting_value", asyncpg.UndefinedTableError("x"))
    for call in (
        service.list_settings(conn),
        service.history(conn, ps.RATE_AI, staff_sub="zz"),
        service.propose(conn, ps.RATE_AI, value=7, reason=REASON, staff_sub="zz", base_version=0),
        service.decide(conn, VID, staff_sub="zz", approve=True),
    ):
        with pytest.raises(ServiceError) as err:
            await call
        assert err.value.status_code == 503 and err.value.code == "settings_unavailable"


@pytest.mark.asyncio
async def test_history_walks_the_previous_values(agency: dict[str, Any]) -> None:
    conn = ScriptedConn().on(
        "ORDER BY v.version DESC",
        [
            _row(version=3, status="rejected", value=2),
            _row(version=2, value=4),
            _row(version=1, status="superseded", value=3),
        ],
    )
    out = await service.history(conn, ps.INVOICE_REMINDER_MAX, staff_sub="zz")
    assert [r["previous_value"] for r in out["history"]] == [4, 3, 5]
    tenant = await service.history(conn, ps.RATE_INTAKE_FORM, TENANT, staff_sub="zz")
    assert tenant["tenant_id"] == str(TENANT) and agency["actor"] == "platform:zz"
    assert agency["ctx"].events[0][0] == "ops_read"
    with pytest.raises(ServiceError) as err:
        await service.history(conn, ps.RATE_AI, TENANT, staff_sub="zz")
    assert err.value.code == "not_tenant_scoped"
    with pytest.raises(ServiceError) as err:
        await service.history(conn, "crm.zz.nothing", staff_sub="zz")
    assert err.value.status_code == 404


@pytest.mark.parametrize(
    ("key", "kw", "code"),
    [
        (ps.RATE_AI, {"value": 6.5}, "out_of_bounds"),
        (ps.RATE_AI, {"value": 7, "reason": "too short"}, "reason_too_short"),
        (ps.RATE_AI, {"value": 7, "tenant_id": TENANT}, "not_tenant_scoped"),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_change_is_refused_before_any_write(key: str, kw: dict[str, Any], code: str) -> None:
    conn = ScriptedConn()
    args = {"value": 7, "reason": REASON, "staff_sub": "zz", "base_version": 0, **kw}
    with pytest.raises(ServiceError) as err:
        await service.propose(conn, key, **args)
    assert err.value.code == code and conn.calls == []


@pytest.mark.asyncio
async def test_a_platform_change_applied_by_its_author_is_a_recorded_self_approval() -> None:
    ps.READER._loaded_at = 0.0
    conn = ScriptedConn()
    conn.on("coalesce(max(version), 0)", 0)
    conn.on(
        "INSERT INTO crm.setting_value",
        _row(
            key=ps.AI_BUDGET_MAX,
            status="proposed",
            decided_by=None,
            decided_at=None,
            proposed_by="platform:zz",
            value=3,
        ),
    )
    conn.on(
        "UPDATE crm.setting_value AS v SET status",
        lambda *a: _row(key=ps.AI_BUDGET_MAX, value=3, proposed_by="platform:zz", decided_by=a[2], self_approved=a[4]),
    )
    out = await service.propose(conn, ps.AI_BUDGET_MAX, value=3, reason=REASON, staff_sub="zz", base_version=0,
                                apply=True)  # fmt: skip
    assert out["self_approved"] is True and out["decided_by"] == "platform:zz"
    assert conn.sql("set_config('app.actor'")[0] == ("platform:zz",)
    assert conn.sql("SET status = 'superseded'")  # older approved rows of the scope go
    assert [f.check for f in ops_alerts.PENDING.drain()] == ["setting_self_approved"]  # a high-impact key
    assert ps.READER._loaded_at < 0  # this process re-reads at once


@pytest.mark.asyncio
async def test_an_agency_change_is_written_in_its_context_with_its_audit(agency: dict[str, Any]) -> None:
    conn = ScriptedConn()
    conn.on("coalesce(max(version), 0)", 1)
    conn.on("tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved'", {"version": 1, "value_text": "900"})
    conn.on(
        "INSERT INTO crm.setting_value",
        _row(key=ps.RATE_INTAKE_FORM, tenant_id=str(TENANT), status="proposed", version=2, value=1200),
    )
    out = await service.propose(conn, ps.RATE_INTAKE_FORM, value=1200, reason=REASON, staff_sub="zz", base_version=1,
                                tenant_id=TENANT)  # fmt: skip
    assert out["status"] == "proposed" and out["tenant_id"] == str(TENANT)
    assert conn.sql("INSERT INTO crm.tenant_settings")  # the agency's settings row (the foreign key) first
    assert agency["ctx"].events == [("setting_change", "allowed", "platform_setting", str(VID), "proposed")]


@pytest.mark.asyncio
async def test_stale_versions_and_open_changes_are_409() -> None:
    conn = ScriptedConn().on("coalesce(max(version), 0)", 0)
    with pytest.raises(ServiceError) as err:
        await service.propose(conn, ps.RATE_AI, value=7, reason=REASON, staff_sub="zz", base_version=2)
    assert err.value.code == "stale_version"
    conn.on("INSERT INTO crm.setting_value", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(ServiceError) as err:
        await service.propose(conn, ps.RATE_AI, value=7, reason=REASON, staff_sub="zz", base_version=0)
    assert err.value.code == "open_change"


@pytest.mark.asyncio
async def test_a_self_approval_waits_for_a_second_person_once_two_person_approval_is_on(agency: dict[str, Any]) -> None:
    conn = ScriptedConn()
    conn.on("WHERE v.id = $1 FOR UPDATE", _row(key=ps.RATE_AI, status="proposed", proposed_by="platform:zz", value=7))
    conn.on("WHERE key = $1 AND tenant_id IS NULL AND status = 'approved'", {"value_text": "true"})
    with pytest.raises(ServiceError) as err:
        await service.decide(conn, VID, staff_sub="zz", approve=True)
    assert err.value.code == "second_person_required"
    # an agency's change: the flag is a platform row, read with the agency context cleared and then put back
    conn.on("current_setting('app.tenant_id', true)", str(TENANT))
    with pytest.raises(ServiceError):
        await service.decide(conn, VID, staff_sub="zz", approve=True, tenant_id=TENANT)
    assert conn.sql("set_config('app.tenant_id', $1, true)") == [("",), (str(TENANT),)]


@pytest.mark.asyncio
async def test_two_person_on_fails_closed() -> None:
    assert await service.two_person_on(ScriptedConn()) is False  # no row: the seed (off)
    assert await service.two_person_on(ScriptedConn().on("tenant_id IS NULL AND status", {"value_text": "x"})) is True
    broken = ScriptedConn().on("tenant_id IS NULL AND status", RuntimeError("boom"))
    assert await service.two_person_on(broken) is True


@pytest.mark.asyncio
async def test_decisions_need_an_open_change_and_a_rejection_note(agency: dict[str, Any]) -> None:
    conn = ScriptedConn()
    with pytest.raises(ServiceError) as err:
        await service.decide(conn, VID, staff_sub="zz", approve=True)
    assert err.value.code == "unknown_change"
    conn.on("WHERE v.id = $1 FOR UPDATE", _row())
    with pytest.raises(ServiceError) as err:
        await service.decide(conn, VID, staff_sub="zz", approve=True)
    assert err.value.code == "already_decided"
    conn.on("WHERE v.id = $1 FOR UPDATE", _row(status="proposed", proposed_by="platform:other"))
    with pytest.raises(ServiceError) as err:
        await service.decide(conn, VID, staff_sub="zz", approve=False, note="no")
    assert err.value.code == "note_required"
    with pytest.raises(ServiceError) as err:
        await service.decide(conn, VID, staff_sub="zz", approve=True)  # the UPDATE found no open row
    assert err.value.code == "already_decided"
    conn.on(
        "UPDATE crm.setting_value AS v SET status",
        _row(key=ps.RATE_INTAKE_FORM, tenant_id=str(TENANT), status="rejected", decision_note="not now"),
    )
    out = await service.decide(conn, VID, staff_sub="zz", approve=False, note="not now", tenant_id=TENANT)
    assert out["status"] == "rejected" and agency["ctx"].events[-1][-1] == "rejected"


@pytest.mark.asyncio
async def test_an_agency_approval_drops_only_that_agency_from_the_cache(agency: dict[str, Any]) -> None:
    other = uuid.uuid4()
    ps._TENANT[str(TENANT)] = ps._TenantRows({}, 0.0)
    ps._TENANT[str(other)] = ps._TenantRows({}, 0.0)
    conn = ScriptedConn()
    conn.on(
        "WHERE v.id = $1",
        _row(key=ps.RATE_INTAKE_FORM, tenant_id=str(TENANT), status="proposed", proposed_by="platform:other"),
    )
    conn.on("UPDATE crm.setting_value AS v SET status", _row(key=ps.RATE_INTAKE_FORM, tenant_id=str(TENANT)))
    await service.decide(conn, VID, staff_sub="zz", approve=True, tenant_id=TENANT)
    assert str(TENANT) not in ps._TENANT and str(other) in ps._TENANT


def test_new_agency_settings_take_todays_platform_defaults() -> None:
    from app.agencies import new_agency_settings

    assert new_agency_settings() == (Decimal("2.00"), Decimal("10"), Decimal("1000.00"), 7, 36, 60)
