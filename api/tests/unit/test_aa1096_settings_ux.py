"""AA-1096 S280 (settings audit, lane crm): the verified findings on the CRM settings, on scripted connections.

- The settings list names the agencies with their own value of a platform+tenant key and their open changes.
- A backdated ``effective_from`` is refused; an approval that a later value already in force would hide is refused.
- A 1-2 character evidence or a short reason is refused before the database (no 500); the definition says the minimums.
- An agency's first own value is diffed against the platform value it replaced, not the compiled default.
- A markup / price-check limit the agency saved before AA lowered the limit stays savable; a new one is checked.
- An agent's refund refusal names the limit; the owner reads and sets the threshold on Settings > Payments.
- Settings > Forms reads the agency's hourly form quota.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app import platform_settings as ps
from app.errors import Forbidden, Invalid, ServiceError
from app.intake import booking_settings
from app.intake import forms as intake_forms
from app.ledger import fx
from app.payments import service as pay
from app.platform_settings import service
from app.platform_settings.reader import _Row
from app.proposals import retail
from app.routers.ops_settings import SettingChangeIn, SettingsOut
from app.settings import Settings
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000003a1")
VID = uuid.UUID("00000000-0000-4000-8000-0000000003b2")
NOW = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.UTC)
REASON = "zz a reason long enough for the check"
TEST = Settings(environment="test", db_ssl="disable")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ps.configure_env({})
    ps.invalidate()
    ps.READER._snapshot = {}
    yield
    ps.configure_env({})
    ps.invalidate()
    ps.READER._snapshot = None


def approved(key: str, value: Any) -> None:
    ps.READER._snapshot[(key, None)] = _Row(value=value, version=2)  # type: ignore[index]


def _row(**over: Any) -> dict[str, Any]:
    value = over.pop("value", 900)
    row = {
        "id": VID,
        "key": ps.RATE_INTAKE_FORM,
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


class _Ctx:
    def __init__(self, conn: ScriptedConn, tenant_id: uuid.UUID) -> None:
        self.conn = conn
        self.tenant_id = tenant_id
        self.events: list[tuple[Any, ...]] = []

    def audit(self, *args: Any, **kw: Any) -> None:
        self.events.append((*args, kw.get("reason")))


@pytest.fixture
def agencies(monkeypatch: pytest.MonkeyPatch) -> dict[uuid.UUID, _Ctx]:
    """Two agencies in the registry; each platform context runs on the same scripted connection."""
    opened: dict[uuid.UUID, _Ctx] = {}
    other = uuid.UUID("00000000-0000-4000-8000-0000000003a2")

    class _Agency:
        def __init__(self, tenant_id: uuid.UUID) -> None:
            self.tenant_id = tenant_id

    async def fake_agencies(_pool: Any) -> list[Any]:
        return [_Agency(TENANT), _Agency(other)]

    @asynccontextmanager
    async def fake_platform_tx(pool: Any, tenant_id: uuid.UUID, *, actor: str, statuses: Any) -> Any:
        ctx = _Ctx(pool, tenant_id)
        opened[tenant_id] = ctx
        yield ctx

    monkeypatch.setattr(service.registry, "agencies", fake_agencies)
    monkeypatch.setattr(service, "platform_tx", fake_platform_tx)
    return opened


# ── the settings list: per-agency values and open changes ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_list_names_agencies_with_their_own_value_and_their_open_changes(
    agencies: dict[uuid.UUID, _Ctx],
) -> None:
    def rows(tenant_id: uuid.UUID, keys: list[str]) -> list[dict[str, Any]]:
        assert set(keys) == {k for k in ps.keys() if ps.definition(k).tenant_scoped}
        if tenant_id != TENANT:
            return []  # the other agency has no row of its own
        return [
            _row(tenant_id=str(TENANT), value=1200, version=3),
            _row(
                key=ps.AI_BUDGET_MAX,
                tenant_id=str(TENANT),
                value=9,
                version=2,
                status="proposed",
                decided_by=None,
                decided_at=None,
            ),
        ]

    conn = ScriptedConn().on("v.key = ANY($2::text[])", rows).on("SELECT display_name FROM crm.tenant", "Zz Travel")
    out = await service.list_settings(conn, staff_sub="zz-staff")
    by_key = {s["key"]: s for s in out["settings"]}
    form = by_key[ps.RATE_INTAKE_FORM]
    assert [(r["tenant_id"], r["agency_name"], r["value"]) for r in form["agency_overrides"]] == [
        (str(TENANT), "Zz Travel", 1200)
    ]
    assert form["agency_pending"] == []
    assert [(r["agency_name"], r["value"], r["status"]) for r in by_key[ps.AI_BUDGET_MAX]["agency_pending"]] == [
        ("Zz Travel", 9, "proposed")
    ]
    assert by_key[ps.RATE_AI]["agency_overrides"] == [] and by_key[ps.RATE_AI]["agency_pending"] == []
    # one ops_read in the agency that had rows, none in the other (nothing of it was shown)
    assert [e[0] for e in agencies[TENANT].events] == ["ops_read"]
    assert len(agencies) == 2 and all(not c.events for t, c in agencies.items() if t != TENANT)
    SettingsOut.model_validate(out)


# ── a change: when it starts, what it carries ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_backdated_change_is_refused_before_any_write() -> None:
    conn = ScriptedConn()
    past = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    with pytest.raises(ServiceError) as err:
        await service.propose(
            conn, ps.RATE_AI, value=7, reason=REASON, staff_sub="zz", base_version=0, effective_from=past
        )
    assert (err.value.status_code, err.value.code) == (422, "effective_from_in_past") and conn.calls == []
    # now (within the clock skew) and later are fine; a naive time is read as UTC
    service.check_effective_from(dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30))
    service.check_effective_from(dt.datetime.now(dt.UTC) + dt.timedelta(days=2))
    service.check_effective_from(None)
    with pytest.raises(ServiceError):
        service.check_effective_from(dt.datetime(2026, 1, 1), now=NOW)


@pytest.mark.asyncio
async def test_an_approval_a_later_value_would_hide_is_refused() -> None:
    conn = ScriptedConn()
    open_change = _row(
        key=ps.RATE_AI, value=7, status="proposed", proposed_by="platform:zz-other", decided_by=None, decided_at=None
    )
    conn.on("WHERE v.id = $1 FOR UPDATE", open_change)
    conn.on("effective_from > $4::timestamptz", 1)
    with pytest.raises(ServiceError) as err:
        await service.decide(conn, VID, staff_sub="zz", approve=True)
    assert (err.value.status_code, err.value.code) == (409, "superseded_by_later")
    assert conn.sql("UPDATE crm.setting_value AS v SET status") == []


@pytest.mark.asyncio
async def test_a_short_evidence_is_a_422_not_a_database_error() -> None:
    conn = ScriptedConn()
    with pytest.raises(ServiceError) as err:
        await service.propose(
            conn, ps.RATE_AI, value=7, reason=REASON, staff_sub="zz", base_version=0, evidence_ref=" ab "
        )
    assert (err.value.status_code, err.value.code) == (422, "evidence_invalid") and conn.calls == []


def test_the_change_body_and_the_definition_carry_the_minimums() -> None:
    SettingChangeIn.model_validate({"value": 7, "reason": REASON, "evidence_ref": "SUP-12"})
    for bad in (
        {"value": 7, "reason": "too short"},
        {"value": 7, "reason": REASON, "evidence_ref": "ab"},
        {"value": 7, "reason": REASON, "evidence_ref": "x" * 501},
    ):
        with pytest.raises(ValidationError):
            SettingChangeIn.model_validate(bad)
    d = service.definition_out(ps.definition(ps.RATE_AI))
    assert (d["reason_min"], d["evidence_min"], d["evidence_max"]) == (20, 3, 500)


# ── history: an agency's first own value against the platform value it replaced ──────────────────


def test_an_agency_history_starts_from_the_platform_value_in_force() -> None:
    d = ps.definition(ps.AI_BUDGET_MAX)
    platform = [
        service.row_out(_row(key=ps.AI_BUDGET_MAX, value=5, version=2, effective_from=NOW - dt.timedelta(days=5))),
        service.row_out(
            _row(
                key=ps.AI_BUDGET_MAX,
                value=3,
                version=1,
                status="superseded",
                effective_from=NOW - dt.timedelta(days=20),
            )
        ),
        service.row_out(_row(key=ps.AI_BUDGET_MAX, value=99, version=3, status="rejected", effective_from=NOW)),
    ]
    agency = [
        service.row_out(_row(key=ps.AI_BUDGET_MAX, tenant_id=str(TENANT), value=12, version=2, effective_from=NOW)),
        service.row_out(
            _row(
                key=ps.AI_BUDGET_MAX,
                tenant_id=str(TENANT),
                value=10,
                version=1,
                status="superseded",
                effective_from=NOW - dt.timedelta(days=1),
            )
        ),
    ]
    out = service._with_previous(agency, d, platform)
    assert [r["previous_value"] for r in out] == [10, 5]  # 5 -> 10 (not default -> 10), then 10 -> 12
    early = [
        service.row_out(
            _row(key=ps.AI_BUDGET_MAX, tenant_id=str(TENANT), value=4, effective_from=NOW - dt.timedelta(days=60))
        )
    ]
    assert service._with_previous(early, d, platform)[0]["previous_value"] == d.default_value


# ── the agency's own values after AA lowered a limit ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_markup_saved_above_a_lowered_ceiling_does_not_block_another_kind() -> None:
    approved(ps.MARKUP_MAX, 100)
    conn = ScriptedConn().on("SELECT retail_markup_pct FROM crm.tenant_settings", '{"trip_plan": 250}')
    ctx = tenant_ctx(conn)
    await retail.update(ctx, {"trip_plan": Decimal("250"), "hotel_room": Decimal("15")})
    assert json.loads(conn.sql("INSERT INTO crm.tenant_settings (retail_markup_pct)")[0][0]) == {
        "trip_plan": 250,
        "hotel_room": 15,
    }
    with pytest.raises(Invalid) as err:  # a new value above the ceiling names its kind and the ceiling
        await retail.update(ctx, {"transfer": Decimal("120")})
    assert err.value.code == "invalid_markup" and err.value.extra == {"kind": "transfer", "max": "100"}
    with pytest.raises(Invalid):  # changing the kept value means meeting the ceiling
        await retail.update(ctx, {"trip_plan": Decimal("200")})


@pytest.mark.asyncio
async def test_a_price_check_limit_kept_outside_a_narrowed_range_stays_savable() -> None:
    approved(ps.PRICE_CHECKS_MAX, 120)
    conn = (
        ScriptedConn()
        .on("SELECT public_booking_enabled", {"public_booking_enabled": True, "public_price_per_minute": 300})
        .on("FROM crm.intake_form", [])
    )
    ctx = tenant_ctx(conn)
    await booking_settings.update(ctx, TEST, None, 300)
    assert conn.sql("SET public_price_per_minute") == [(300,)]
    with pytest.raises(Invalid) as err:
        await booking_settings.update(ctx, TEST, None, 200)
    assert err.value.code == "invalid_price_checks_per_minute" and err.value.extra == {"min": 10, "max": 120}


# ── the refund owner-approval threshold ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_owner_reads_and_sets_the_refund_threshold() -> None:
    conn = ScriptedConn().on(
        "SELECT base_currency, booking_validation_level, fx_mode",
        {"base_currency": "VND", "booking_validation_level": "basic", "fx_mode": "automatic"},
    )
    conn.on("SELECT refund_owner_threshold_base", Decimal("1000"))
    ctx = tenant_ctx(conn)
    out = await fx.payment_settings(ctx)
    assert (out["base_currency"], out["refund_owner_threshold_base"]) == ("VND", "1000.00")
    await fx.update_payment_settings(ctx, {"refund_owner_threshold_base": Decimal("2500000")})
    assert conn.sql("SET refund_owner_threshold_base = $1") == [(Decimal("2500000.00"),)]
    approved(ps.REFUND_THRESHOLD_DEFAULT, 1500)
    no_row = tenant_ctx(ScriptedConn())
    assert (await fx.payment_settings(no_row))["refund_owner_threshold_base"] == "1500.00"


@pytest.mark.asyncio
async def test_an_agent_refusal_names_the_limit() -> None:
    from tests.unit.test_payments import REFUND, _conn, _refund

    conn = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund()).on("refund_owner_threshold_base", Decimal("100"))
    with pytest.raises(Forbidden) as err:
        await pay.decide_refund(tenant_ctx(conn, role="agent"), REFUND, approve=True, note=None)
    assert err.value.code == "above_threshold"
    assert err.value.extra == {"limit": "100.00", "limit_currency": "USD"}


# ── Settings > Forms: the hourly quota ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_forms_page_reads_the_agency_form_quota() -> None:
    ctx = tenant_ctx(ScriptedConn().on("WHERE v.tenant_id = $1", []))
    assert await intake_forms.form_quota(ctx) == {"submissions_per_hour": 200, "submissions_per_hour_own": False}
    ps.forget_tenant()  # the next context is the same agency: drop its cached rows
    own = tenant_ctx(
        ScriptedConn().on("WHERE v.tenant_id = $1", [{"key": ps.RATE_INTAKE_FORM, "value_text": "900", "version": 2}])
    )
    assert await intake_forms.form_quota(own) == {"submissions_per_hour": 900, "submissions_per_hour_own": True}
