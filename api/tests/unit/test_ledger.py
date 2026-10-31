"""AA-944 ledger services with a scripted connection: the refusal wording, money shaped as keyed objects (margin
serializer), the assistant's confirm (R18: success + owner task), the strictness answers, the waiting_on hook, the
supplier directory and the payment settings. The SQL, RLS and definer functions run on PostgreSQL in
tests/integration/test_crm014_schema.py and test_ledger_api.py."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import stages
from app.errors import Conflict, Forbidden, Invalid, NotFound, ServiceError
from app.ledger import fx, lines, refusal, suppliers
from app.margin import strip_margin
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

LINE = uuid.UUID("00000000-0000-4000-8000-0000000001e1")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000001f1")
SUPPLIER = uuid.UUID("00000000-0000-4000-8000-0000000005a1")
NOW = dt.datetime(2026, 10, 24, 3, 0, tzinfo=dt.UTC)


@contextmanager
def _raises(cls: type[ServiceError], code: str) -> Iterator[None]:
    """pytest.raises on the service error's code (its message is for people)."""
    with pytest.raises(cls) as err:
        yield
    assert err.value.code == code


def _db_error(sqlstate: str, message: str) -> asyncpg.PostgresError:
    exc = asyncpg.PostgresError(message)
    exc.sqlstate = sqlstate  # type: ignore[misc]
    return exc


def _line_row(**over: Any) -> dict[str, Any]:
    row = {
        "booking_line_id": LINE,
        "trip_id": TRIP,
        "supplier_kind": "agency",
        "agency_supplier_id": SUPPLIER,
        "supplier_name": "Hanoi Pearl",
        "status": "planning",
        "aa_sub_status": None,
        "title": "Hanoi 2 nights",
        "confirmation_no": "HP-1",
        "service_from": dt.date(2026, 11, 2),
        "service_to": dt.date(2026, 11, 4),
        "collect": "agency",
        "confirm_class": None,
        "visible_to_travelers": False,
        "confirmed_at": None,
        "departure_id": None,
        "created_at": NOW,
        "updated_at": NOW,
        "trip_title": "Vietnam 9 nights",
        "trip_stage": "booked",
    }
    row.update(over)
    return row


def _money_row(kind: str, amount: str, frozen: bool = True) -> dict[str, Any]:
    return {
        "booking_line_id": LINE,
        "kind": kind,
        "amount": Decimal(amount),
        "currency": "VND",
        "base_currency": "USD" if frozen else None,
        "amount_base": (Decimal(amount) / 25000).quantize(Decimal("0.01")) if frozen else None,
        "fx_rate": Decimal("0.00004") if frozen else None,
        "fx_rate_date": dt.date(2026, 10, 24) if frozen else None,
        "fx_source": "platform_daily" if frozen else None,
        "stale_days": 0 if frozen else None,
    }


def _conn(**over: Any) -> ScriptedConn:
    conn = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id", "booked")
        .on("WHERE l.trip_id = $1", [_line_row()])
        .on("WHERE l.booking_line_id = $1", _line_row())
        .on("WHERE l.deleted_at IS NULL AND", [_line_row()])
        .on(
            "SELECT trip_id, status, supplier_kind FROM crm.booking_line",
            {"trip_id": TRIP, "status": "planning", "supplier_kind": "agency"},
        )
        .on("v_booking_line_money_current", [_money_row("cost", "2500000"), _money_row("sell", "3000000")])
        .on("SELECT base_currency, booking_validation_level, fx_mode", None)
        # AA-957: the commission follow-up of the trip's lines and the cancellation fees of the ledger totals.
        .on("FROM crm.commission c", [])
        .on("m.kind = 'cancellation_cost'", Decimal("7.50"))
    )
    for fragment, answer in over.items():
        conn.on(fragment, answer)
    return conn


# ── refusals ─────────────────────────────────────────────────────────────────────────────────────


def test_refusals_are_worded_with_their_details() -> None:
    incomplete = refusal(_db_error("CRM23", "crm: line_incomplete:confirmation_no,cost"))
    assert isinstance(incomplete, Conflict) and incomplete.extra == {"missing": ["confirmation_no", "cost"]}
    missing = refusal(_db_error("CRM25", "crm: fx_rate_missing:QXX (no rate)"))
    assert missing is not None and missing.code == "fx_rate_missing" and missing.extra == {"currency": "QXX"}
    assert isinstance(refusal(_db_error("CRM24", "crm: aa_line_read_only")), Forbidden)
    assert refusal(_db_error("CRM22", "x")).code == "aa_supplier_builtin"  # type: ignore[union-attr]
    assert refusal(_db_error("CRM27", "x")).code == "supplier_in_use"  # type: ignore[union-attr]
    assert refusal(_db_error("23514", "check")) is None
    dup = asyncpg.UniqueViolationError("duplicate key")
    dup.constraint_name = "booking_line_conf_uq"
    assert refusal(dup).code == "duplicate_confirmation_no"  # type: ignore[union-attr]
    margin = asyncpg.InsufficientPrivilegeError("crm: margin_forbidden (only an owner or an agent)")
    assert refusal(margin).code == "role_forbidden"  # type: ignore[union-attr]


# ── reads: money keyed, the assistant never gets it ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_gets_money_as_keyed_objects_with_the_margin() -> None:
    conn = _conn()
    out = await lines.trip_lines(tenant_ctx(conn, role="owner"), TRIP)
    money = out["lines"][0]["money"]
    assert set(money) == {"cost", "sell", "commission_expected", "margin_base"}
    assert money["margin_base"] == "20.00" and money["commission_expected"] is None
    assert money["sell"]["fx"]["attribution"]["text"] == "Rates By Exchange Rate API"
    assert out["base_currency"] == "USD" and out["booking_validation_level"] == "basic"
    assert out["lines"][0]["commission"] is None  # AA-957: no commission row yet
    # The serializer's view for the assistant: no cost / commission / margin anywhere.
    stripped = strip_margin(out)
    assert set(stripped["lines"][0]["money"]) == {"sell"} and "commission" not in stripped["lines"][0]


@pytest.mark.asyncio
async def test_the_assistant_gets_no_money_block_and_no_money_query() -> None:
    conn = _conn()
    out = await lines.trip_lines(tenant_ctx(conn, role="assistant"), TRIP)
    assert "money" not in out["lines"][0]
    assert conn.sql("v_booking_line_money_current") == []


@pytest.mark.asyncio
async def test_an_unknown_trip_or_line_is_404() -> None:
    conn = _conn(**{"FROM crm.trip WHERE trip_id": None, "WHERE l.booking_line_id = $1": None})
    with _raises(NotFound, "trip_not_found"):
        await lines.trip_lines(tenant_ctx(conn), TRIP)
    with _raises(NotFound, "line_not_found"):
        await lines.line(tenant_ctx(conn), LINE)


@pytest.mark.asyncio
async def test_planning_money_has_no_fx_block_and_no_margin() -> None:
    conn = _conn(v_booking_line_money_current=[_money_row("cost", "1", frozen=False), _money_row("sell", "2", False)])
    money = (await lines.line(tenant_ctx(conn, role="agent"), LINE))["money"]
    assert money["cost"] == {"amount": "1", "currency": "VND"} and "margin_base" not in money


@pytest.mark.asyncio
async def test_the_ledger_has_totals_for_owner_and_agent_only() -> None:
    totals = {
        "confirmed_sell_base": Decimal("120.00"),
        "confirmed_cost_base": Decimal("100.00"),
        "confirmed_commission_expected_base": Decimal("5.00"),
        "pipeline_sell_base": Decimal("40.00"),
        "pipeline_unconverted": 1,
        "confirmed_other_base": 2,
    }
    conn = _conn(**{"WITH heads AS": totals, "now() AT TIME ZONE": dt.date(2026, 10, 24)})
    ctx = tenant_ctx(conn, role="agent")
    out = await lines.ledger(ctx, status="confirmed", date_from=None, date_to=None)
    assert out["totals"]["confirmed_margin_base"] == "20.00" and out["totals"]["pipeline_unconverted"] == 1
    assert out["totals"]["confirmed_other_base"] == 2  # lines frozen in an earlier base: counted apart
    assert out["totals"]["cancellation_cost_base"] == "7.50"  # AA-957: AA's fees of cancelled bookings
    (totals_call,) = [c for c in conn.calls if "WITH heads AS" in c[0]]
    assert "base_currency = $3" in totals_call[0] and totals_call[1][2] == "USD"
    assert out["lines"][0]["trip_title"] == "Vietnam 9 nights"
    assert ctx.audit_events[-1].action == "list_view"
    assistant = await lines.ledger(tenant_ctx(_conn(), role="assistant"), status=None, date_from=None, date_to=None)
    assert "totals" not in assistant and "money" not in assistant["lines"][0]
    with pytest.raises(Invalid):
        await lines.ledger(ctx, status="booked", date_from=None, date_to=None)


# ── writes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_line_needs_an_own_supplier() -> None:
    body = {"agency_supplier_id": str(SUPPLIER), "title": "Hanoi 2 nights", "collect": "agency"}
    aa = _conn(**{"SELECT kind, deleted_at FROM crm.agency_supplier": {"kind": "aa", "deleted_at": None}})
    with _raises(Forbidden, "aa_line_read_only"):
        await lines.create_line(tenant_ctx(aa), TRIP, body)
    gone = _conn(**{"SELECT kind, deleted_at FROM crm.agency_supplier": None})
    with _raises(Invalid, "invalid_agency_supplier_id"):
        await lines.create_line(tenant_ctx(gone), TRIP, body)
    ok = _conn(**{"SELECT kind, deleted_at FROM crm.agency_supplier": {"kind": "own", "deleted_at": None}})
    ok.on("INSERT INTO crm.booking_line", LINE)
    ctx = tenant_ctx(ok, role="assistant")
    out = await lines.create_line(ctx, TRIP, {**body, "service_from": "2026-11-02", "service_to": "2026-11-04"})
    assert out["booking_line_id"] == str(LINE) and ctx.audit_events[0].action == "create"
    with _raises(Invalid, "invalid_service_to"):
        await lines.create_line(ctx, TRIP, {**body, "service_from": "2026-11-04", "service_to": "2026-11-02"})
    with _raises(Invalid, "invalid_title"):
        await lines.create_line(ctx, TRIP, {**body, "title": " "})


@pytest.mark.asyncio
async def test_update_line_validates_each_field() -> None:
    conn = _conn(**{"SELECT kind, deleted_at FROM crm.agency_supplier": {"kind": "own", "deleted_at": None}})
    ctx = tenant_ctx(conn)
    await lines.update_line(
        ctx,
        LINE,
        {
            "title": "Hanoi 3 nights",
            "confirmation_no": "",
            "service_from": "2026-11-01",
            "collect": "on_arrival",
            "confirm_class": None,
            "visible_to_travelers": True,
            "agency_supplier_id": str(SUPPLIER),
        },
    )
    (update,) = [c for c in conn.calls if c[0].startswith("UPDATE crm.booking_line SET")]
    assert "agency_supplier_id = $8" in update[0] and update[1][2] is None  # an empty number clears it
    for bad in ({"collect": "cash"}, {"visible_to_travelers": "yes"}, {"service_from": "soon"}):
        with pytest.raises(Invalid):
            await lines.update_line(ctx, LINE, bad)
    confirmed = _conn(
        **{
            "SELECT trip_id, status, supplier_kind FROM crm.booking_line": {
                "trip_id": TRIP,
                "status": "confirmed",
                "supplier_kind": "agency",
            },
            "SELECT kind, deleted_at FROM crm.agency_supplier": {"kind": "own", "deleted_at": None},
        }
    )
    with _raises(Invalid, "line_not_editable"):
        await lines.update_line(tenant_ctx(confirmed), LINE, {"agency_supplier_id": str(SUPPLIER)})
    dates = _conn(**{"UPDATE crm.booking_line SET": asyncpg.CheckViolationError("dates")})
    dates.rules[0] = ("UPDATE crm.booking_line SET", _check("booking_line_dates_chk"))
    with _raises(Invalid, "invalid_service_to"):
        await lines.update_line(tenant_ctx(dates), LINE, {"service_to": "2026-10-01"})
    aa = _conn(**{"UPDATE crm.booking_line SET": _db_error("CRM24", "crm: aa_line_read_only")})
    with _raises(Forbidden, "aa_line_read_only"):
        await lines.update_line(tenant_ctx(aa), LINE, {"title": "x"})


@pytest.mark.asyncio
async def test_a_confirmed_line_keeps_its_number_unless_the_level_is_off() -> None:
    confirmed = {"trip_id": TRIP, "status": "confirmed", "supplier_kind": "agency"}
    conn = _conn(**{"SELECT trip_id, status, supplier_kind FROM crm.booking_line": confirmed})
    with pytest.raises(Conflict) as err:
        await lines.update_line(tenant_ctx(conn), LINE, {"confirmation_no": ""})
    assert err.value.code == "line_incomplete" and err.value.extra["missing"] == ["confirmation_no"]
    assert not [c for c in conn.calls if c[0].startswith("UPDATE crm.booking_line SET")]
    await lines.update_line(tenant_ctx(conn), LINE, {"confirmation_no": "HP-2"})  # a new number is fine
    off = _conn(
        **{
            "SELECT trip_id, status, supplier_kind FROM crm.booking_line": confirmed,
            "SELECT base_currency, booking_validation_level, fx_mode": {
                "base_currency": "USD",
                "booking_validation_level": "off",
                "fx_mode": "automatic",
            },
        }
    )
    await lines.update_line(tenant_ctx(off), LINE, {"confirmation_no": ""})
    # The database refuses it too (CRM23): worded the same way.
    db = _conn(**{"UPDATE crm.booking_line SET": _db_error("CRM23", "crm: line_incomplete:confirmation_no (kept)")})
    with pytest.raises(Conflict) as err:
        await lines.update_line(tenant_ctx(db), LINE, {"confirmation_no": "", "title": "x"})
    assert err.value.extra["missing"] == ["confirmation_no"]


def _check(name: str) -> asyncpg.CheckViolationError:
    exc = asyncpg.CheckViolationError("check")
    exc.constraint_name = name
    return exc


@pytest.mark.asyncio
async def test_the_assistants_confirm_without_commission_succeeds_and_opens_an_owner_task() -> None:
    owner = uuid.uuid4()
    task = uuid.uuid4()
    conn = _conn(
        **{
            "crm.booking_line_confirm($1)": '{"frozen": 2, "stale_days": 0, "pending": ["commission"]}',
            "FROM crm.member WHERE role = 'owner'": owner,
            "INSERT INTO crm.task": task,
        }
    )
    ctx = tenant_ctx(conn, role="assistant")
    out = await lines.confirm(ctx, LINE)
    assert out["owner_task"] == {"kind": "add_commission", "task_id": str(task)} and out["frozen"] == 2
    assert conn.sql("INSERT INTO crm.task") == [(TRIP, "add_commission", owner)]
    # An owner never gets the task: the database refuses a missing commission under strict (409, below).
    conn = _conn(**{"crm.booking_line_confirm($1)": {"frozen": 0, "stale_days": 0, "pending": []}})
    assert "owner_task" not in await lines.confirm(tenant_ctx(conn, role="owner"), LINE)
    assert conn.sql("INSERT INTO crm.task") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (_db_error("CRM23", "crm: line_incomplete:commission"), "line_incomplete"),
        (_db_error("CRM26", "crm: line_not_editable"), "line_not_editable"),
        (_db_error("CRM25", "crm: fx_rate_missing:QXX"), "fx_rate_missing"),
    ],
)
async def test_a_refused_confirm_is_a_409(error: Exception, code: str) -> None:
    conn = _conn(**{"crm.booking_line_confirm($1)": error})
    with pytest.raises(Conflict) as err:
        await lines.confirm(tenant_ctx(conn, role="agent"), LINE)
    assert err.value.code == code


@pytest.mark.asyncio
async def test_a_duplicate_number_under_strict_is_409_duplicate_confirmation_no() -> None:
    dup = asyncpg.UniqueViolationError("duplicate key")
    dup.constraint_name = "booking_line_conf_uq"
    conn = _conn(**{"crm.booking_line_confirm($1)": dup})
    with _raises(Conflict, "duplicate_confirmation_no"):
        await lines.confirm(tenant_ctx(conn), LINE)
    other = asyncpg.UniqueViolationError("something else")
    with pytest.raises(asyncpg.UniqueViolationError):
        await lines.confirm(tenant_ctx(_conn(**{"crm.booking_line_confirm($1)": other})), LINE)


@pytest.mark.asyncio
async def test_confirm_check_reports_what_blocks_each_role() -> None:
    conn = _conn(**{"crm.booking_line_confirm_check($1)": ["cost", "commission"]})
    assert (await lines.confirm_check(tenant_ctx(conn, role="assistant"), LINE))["blocking"] == ["cost"]
    assert (await lines.confirm_check(tenant_ctx(conn, role="agent"), LINE))["blocking"] == ["cost", "commission"]


@pytest.mark.asyncio
async def test_cancel_and_trash_rules() -> None:
    conn = _conn(**{"UPDATE crm.booking_line SET status = 'cancelled'": LINE})
    assert (await lines.cancel(tenant_ctx(conn), LINE))["booking_line_id"] == str(LINE)
    with _raises(Invalid, "line_not_editable"):
        await lines.cancel(tenant_ctx(_conn()), LINE)
    await lines.trash_check(tenant_ctx(_conn()), LINE)
    for status, kind, err in (("confirmed", "agency", Conflict), ("planning", "aa", Forbidden)):
        row = {"trip_id": TRIP, "status": status, "supplier_kind": kind}
        with pytest.raises(err):
            await lines.trash_check(tenant_ctx(_conn(**{"SELECT trip_id, status, supplier_kind": row})), LINE)
    with pytest.raises(NotFound):
        await lines.trash_check(tenant_ctx(_conn(**{"SELECT trip_id, status, supplier_kind": None})), LINE)


@pytest.mark.asyncio
async def test_put_money_writes_one_head_per_kind() -> None:
    conn = _conn()
    ctx = tenant_ctx(conn, role="agent")
    body = {"cost": {"amount": "100", "currency": "USD"}, "sell": {"amount": "130.456", "currency": "USD"}}
    await lines.put_money(ctx, LINE, body)
    assert conn.sql("crm.booking_line_money_put") == [
        (LINE, "cost", Decimal("100.00"), "USD"),
        (LINE, "sell", Decimal("130.46"), "USD"),
    ]
    for bad in (
        {},
        {"cost": 5},
        {"cost": {"amount": "-1", "currency": "USD"}},
        {"sell": {"amount": "1", "currency": "usd"}},
    ):
        with pytest.raises(Invalid):
            await lines.put_money(ctx, LINE, bad)
    refused = _conn(**{"crm.booking_line_money_put": _db_error("CRM26", "crm: line_not_editable")})
    with pytest.raises(Conflict):
        await lines.put_money(tenant_ctx(refused), LINE, body)


@pytest.mark.asyncio
async def test_the_waiting_on_hook_answers_supplier_for_open_agency_lines() -> None:
    assert any(name == "supplier_lines" and prio == 50 for prio, name, _ in stages.WAITING_ON_HOOKS)
    facts = stages.TripFacts(trip_id=TRIP, stage="booked", stage2_link_open=False)
    assert (
        await lines.supplier_lines_hook(tenant_ctx(ScriptedConn().on("status = 'planning'", True)), facts) == "supplier"
    )
    assert await lines.supplier_lines_hook(tenant_ctx(ScriptedConn().on("status = 'planning'", False)), facts) is None


# ── suppliers ────────────────────────────────────────────────────────────────────────────────────


def _supplier_row(**over: Any) -> dict[str, Any]:
    row = {
        "agency_supplier_id": SUPPLIER,
        "kind": "own",
        "name": "Hanoi Pearl",
        "category": "accommodation",
        "default_commission_pct": Decimal("10.00"),
        "commission_due_days": 45,
        "confirmation_unique": False,
        "has_contact_name": True,
        "has_contact_email": False,
        "line_count": 2,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_the_supplier_list_makes_sure_aa_exists_and_is_one_list_view() -> None:
    conn = ScriptedConn().on("crm.ensure_aa_supplier()", SUPPLIER).on("FROM crm.agency_supplier s", [_supplier_row()])
    ctx = tenant_ctx(conn, role="assistant")
    out = await suppliers.list_suppliers(ctx)
    assert out["suppliers"][0]["default_commission_pct"] == "10.00"
    assert "default_commission_pct" not in strip_margin(out)["suppliers"][0]  # the serializer's assistant view
    assert [e.action for e in ctx.audit_events] == ["list_view"] and conn.sql("ensure_aa_supplier")


@pytest.mark.asyncio
async def test_create_supplier_seals_the_contact_fields() -> None:
    conn = ScriptedConn().on("FROM crm.agency_supplier s", _supplier_row())
    ctx = tenant_ctx(conn)
    body = {"name": " Hanoi Pearl ", "category": "accommodation", "contact_name": "Lan", "contact_email": "lan@x.test"}
    await suppliers.create_supplier(ctx, FakeVault(), body)  # type: ignore[arg-type]
    ((_id, name, category, name_enc, email_enc, pct, unique, due_days),) = conn.sql("INSERT INTO crm.agency_supplier")
    assert name == "Hanoi Pearl" and name_enc.startswith(b"enc:agency_supplier.contact_name_enc:")
    assert email_enc.endswith(b"lan@x.test") and pct is None and unique is False and due_days is None
    await suppliers.create_supplier(ctx, FakeVault(), {"name": "x", "commission_due_days": 60})  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.agency_supplier")[-1][-1] == 60  # AA-957: the supplier's commission terms
    for bad in ({"name": ""}, {"name": "x", "category": "bank"}, {"name": "x", "contact_email": "no-at"}):
        with pytest.raises(Invalid):
            await suppliers.create_supplier(ctx, FakeVault(), bad)  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await suppliers.create_supplier(ctx, FakeVault(), {"name": "x", "default_commission_pct": 101})  # type: ignore[arg-type]
    for days in (-1, 366, True, "30"):
        with pytest.raises(Invalid):
            await suppliers.create_supplier(ctx, FakeVault(), {"name": "x", "commission_due_days": days})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_aa_row_is_never_edited_and_contacts_open_with_a_reason() -> None:
    aa = ScriptedConn().on("SELECT kind, deleted_at, contact_name_enc", {"kind": "aa", "deleted_at": None})
    with _raises(Conflict, "aa_supplier_builtin"):
        await suppliers.update_supplier(tenant_ctx(aa), FakeVault(), SUPPLIER, {"name": "AA"})  # type: ignore[arg-type]
    with _raises(Conflict, "aa_supplier_builtin"):
        await suppliers.trash_check(tenant_ctx(aa), SUPPLIER)
    row = {
        "kind": "own",
        "deleted_at": None,
        "contact_name_enc": b"enc:agency_supplier.contact_name_enc:Lan",
        "contact_email_enc": None,
    }
    conn = (
        ScriptedConn()
        .on("SELECT kind, deleted_at, contact_name_enc", row)
        .on("FROM crm.agency_supplier s", _supplier_row())
    )
    ctx = tenant_ctx(conn)
    changes = {"name": "Pearl", "category": "other", "default_commission_pct": None, "confirmation_unique": True}
    await suppliers.update_supplier(ctx, FakeVault(), SUPPLIER, {**changes, "contact_email": "a@b.test"})  # type: ignore[arg-type]
    assert any(c[0].startswith("UPDATE crm.agency_supplier SET name = $2") for c in conn.calls)
    with pytest.raises(Invalid):
        await suppliers.update_supplier(ctx, FakeVault(), SUPPLIER, {"confirmation_unique": "yes"})  # type: ignore[arg-type]
    with _raises(Invalid, "reason_required"):
        await suppliers.reveal_contact(ctx, FakeVault(), SUPPLIER, "contact_name", "curious")  # type: ignore[arg-type]
    with _raises(Invalid, "invalid_field"):
        await suppliers.reveal_contact(ctx, FakeVault(), SUPPLIER, "phone", "other")  # type: ignore[arg-type]
    shown = await suppliers.reveal_contact(ctx, FakeVault(), SUPPLIER, "contact_name", "prepare_booking")  # type: ignore[arg-type]
    assert shown["value"] == "Lan" and ctx.audit_events[-1].action == "reveal"
    empty = await suppliers.reveal_contact(ctx, FakeVault(), SUPPLIER, "contact_email", "other")  # type: ignore[arg-type]
    assert empty["value"] is None
    in_use = ScriptedConn().on("SELECT kind, deleted_at, contact_name_enc", row).on("SELECT count(*)", 3)
    with _raises(Conflict, "supplier_in_use"):
        await suppliers.trash_check(tenant_ctx(in_use), SUPPLIER)
    with pytest.raises(NotFound):
        await suppliers.trash_check(tenant_ctx(ScriptedConn()), SUPPLIER)
    with pytest.raises(NotFound):
        await suppliers.get_supplier(tenant_ctx(ScriptedConn()), SUPPLIER)


# ── payment settings and overrides ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_payment_settings_default_and_update() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    got = await fx.payment_settings(ctx)
    assert (got["base_currency"], got["booking_validation_level"], got["fx_mode"]) == ("USD", "basic", "automatic")
    await fx.update_payment_settings(ctx, {"booking_validation_level": "strict", "base_currency": "VND"})
    assert conn.sql("INSERT INTO crm.tenant_settings") == [("VND", "strict", "automatic")]
    for bad in ({"base_currency": "vnd"}, {"booking_validation_level": "loose"}, {"fx_mode": "auto"}):
        with pytest.raises(Invalid):
            await fx.update_payment_settings(ctx, bad)


@pytest.mark.asyncio
async def test_overrides_are_checked_before_they_are_stored() -> None:
    today = dt.date(2026, 10, 24)
    conn = (
        ScriptedConn()
        .on("now() AT TIME ZONE", today)
        .on("INSERT INTO crm.fx_rate_override", {"fx_rate_override_id": uuid.uuid4(), "created_at": NOW})
    )
    ctx = tenant_ctx(conn)
    out = await fx.add_override(ctx, base="USD", quote="VND", rate="25500", rate_date=None)
    assert out["rate_date"] == "2026-10-24" and out["rate"] == "25500"
    for kwargs in (
        {"base": "USD", "quote": "USD", "rate": 1, "rate_date": None},
        {"base": "USD", "quote": "VND", "rate": 0, "rate_date": None},
        {"base": "USD", "quote": "VND", "rate": "x", "rate_date": None},
        {"base": "USD", "quote": "VND", "rate": 1, "rate_date": today + dt.timedelta(days=1)},
    ):
        with pytest.raises(Invalid):
            await fx.add_override(ctx, **kwargs)
    rows = [{**_override(), "set_by": None}]
    assert (await fx.list_overrides(tenant_ctx(ScriptedConn().on("FROM crm.fx_rate_override", rows))))["overrides"]


def _override() -> dict[str, Any]:
    return {
        "fx_rate_override_id": uuid.uuid4(),
        "rate_date": dt.date(2026, 10, 24),
        "base": "USD",
        "quote": "VND",
        "rate": Decimal("25500"),
        "created_at": NOW,
    }


@pytest.mark.asyncio
async def test_resolve_wraps_the_database_answer() -> None:
    conn = ScriptedConn().on(
        "crm.fx_resolve",
        {
            "rate": Decimal("0.00004"),
            "rate_date": dt.date(2026, 10, 23),
            "fx_source": "platform_daily",
            "stale_days": 1,
        },
    )
    rate = await fx.resolve(tenant_ctx(conn), "VND", "USD", dt.date(2026, 10, 24))
    assert rate is not None and rate.view()["stale_days"] == 1 and "attribution" in rate.view()
    assert await fx.resolve(tenant_ctx(ScriptedConn()), "VND", "USD", dt.date(2026, 10, 24)) is None
