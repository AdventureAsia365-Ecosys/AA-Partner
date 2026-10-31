"""AA-957 (card CRM-27, design v3 M13, A-12) without a database: :mod:`app.commissions.service` on a scripted
connection. Claim / withdraw claim / receive / write off / due date accept only the statuses they move from and
refuse bad input before any write; the list passes its filter to the SQL and reports the totals; the date rule syncs
the trips it finds and opens one overdue task per trip. The SQL runs on PostgreSQL in
``tests/integration/test_crm035_schema.py`` and ``test_trip_cancel_api.py``."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.cancellation import SUPPLIER_TASK
from app.commissions import service
from app.errors import Conflict, Invalid, NotFound
from app.ledger import fx
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, system_ctx, tenant_ctx

COM = uuid.UUID("00000000-0000-4000-8000-00000000c001")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000007e1")
TRIP2 = uuid.UUID("00000000-0000-4000-8000-0000000007e2")
LINE = uuid.UUID("00000000-0000-4000-8000-0000000001a1")
TODAY = dt.date(2026, 11, 1)
ACTOR = f"member:{MEMBER}"


def flat(sql: str) -> str:
    return " ".join(sql.split())


def row(**over: Any) -> dict[str, Any]:
    base = {
        "commission_id": COM, "trip_id": TRIP, "trip_title": "Highlands", "trip_stage": "booked",
        "booking_line_id": LINE, "line_title": "Lodge", "line_status": "confirmed",
        "service_from": dt.date(2026, 12, 1), "service_to": dt.date(2026, 12, 3),
        "agency_supplier_id": uuid.UUID(int=5),
        "supplier_name": "Riverside", "status": "expected", "due_on": dt.date(2026, 12, 31),
        "claimed_at": None, "claim_ref": None, "close_reason": None, "note": None, "keep_on_cancel": False,
        "expected_amount": Decimal("40"), "expected_currency": "EUR", "expected_amount_base": Decimal("43.2"),
        "expected_base_currency": "USD", "received_amount": None, "received_currency": None, "received_on": None,
        "received_ref": None, "received_amount_base": None, "base_currency": None, "fx_rate": None,
        "fx_rate_date": None, "fx_source": None, "stale_days": None, "short_amount": None,
        "fx_difference_base": None, "updated_at": dt.datetime(2026, 10, 30, 8, tzinfo=dt.UTC),
    }  # fmt: skip
    base.update(over)
    return base


def world(locked: str | None = "expected", **view: Any) -> ScriptedConn:
    """A commission locked in status ``locked`` (None: no such row) whose view reads ``view``."""
    conn = ScriptedConn()
    conn.on(flat(fx.TODAY_SQL), TODAY)
    conn.on(flat(service.SYNC_SQL), {"created": 0})
    conn.on(flat(service.ONE_SQL), row(**view))
    conn.on(flat(service.LOCK_SQL), None if locked is None else {"commission_id": COM, "trip_id": TRIP,
                                                                  "status": locked})  # fmt: skip
    return conn


def writes(conn: ScriptedConn) -> list[str]:
    return [sql for sql, _ in conn.calls if sql.startswith("UPDATE")]


# ── claim / withdraw claim ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_claim_stores_the_reference_syncs_and_audits() -> None:
    conn = world("expected", status="claimed", claim_ref="SUP-7")
    ctx = tenant_ctx(conn, role="agent")
    out = await service.claim(ctx, COM, "  SUP-7 ")
    assert conn.sql(flat(service.CLAIM_SQL)) == [(COM, "SUP-7", ACTOR)]
    assert conn.sql(flat(service.SYNC_SQL)) == [(TRIP,)]
    assert out["status"] == "claimed" and out["claim_ref"] == "SUP-7"
    assert [(e.action, e.object_id) for e in ctx.audit_events] == [("commission_claim", str(COM))]
    # A claim without a reference is fine (blank is none).
    conn = world("expected")
    await service.claim(tenant_ctx(conn), COM, "   ")
    assert conn.sql(flat(service.CLAIM_SQL)) == [(COM, None, ACTOR)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [("claimed", "commission_not_claimed"), ("received", "commission_closed"), ("written_off", "commission_closed"),
     ("cancelled", "commission_closed")],
)  # fmt: skip
async def test_only_an_expected_commission_can_be_claimed(status: str, code: str) -> None:
    conn = world(status)
    with pytest.raises(Conflict) as err:
        await service.claim(tenant_ctx(conn), COM, "SUP-7")
    assert err.value.code == code
    assert writes(conn) == []


@pytest.mark.asyncio
async def test_a_bad_reference_or_a_missing_commission_is_refused_before_writing() -> None:
    for bad in ("x" * 81, "two\nlines", 12):
        conn = world("expected")
        with pytest.raises(Invalid) as err:
            await service.claim(tenant_ctx(conn), COM, bad)
        assert err.value.code == "invalid_reference" and writes(conn) == []
    conn = world(None)
    with pytest.raises(NotFound):
        await service.claim(tenant_ctx(conn), COM, "SUP-7")
    assert writes(conn) == []


@pytest.mark.asyncio
async def test_withdrawing_a_claim_needs_a_claimed_commission() -> None:
    conn = world("claimed")
    ctx = tenant_ctx(conn)
    out = await service.unclaim(ctx, COM)
    assert conn.sql(flat(service.UNCLAIM_SQL)) == [(COM, ACTOR)]
    assert out["status"] == "expected" and ctx.audit_events[-1].action == "commission_unclaim"
    conn = world("expected")
    with pytest.raises(Conflict) as err:
        await service.unclaim(tenant_ctx(conn), COM)
    assert err.value.code == "commission_not_claimed" and writes(conn) == []


# ── receive ──────────────────────────────────────────────────────────────────────────────────────


RECEIVED = {
    "status": "received", "received_amount": Decimal("35"), "received_currency": "EUR",
    "received_on": dt.date(2026, 10, 30), "received_ref": "BANK-1", "received_amount_base": Decimal("37.8"),
    "base_currency": "USD", "fx_rate": Decimal("1.08"), "fx_rate_date": dt.date(2026, 10, 30),
    "fx_source": "platform_daily", "stale_days": 0, "short_amount": Decimal("5"),
    "fx_difference_base": Decimal("-5.4"),
}  # fmt: skip


@pytest.mark.asyncio
async def test_a_receipt_on_a_past_day_is_stored_at_that_days_rate() -> None:
    conn = world("claimed", **RECEIVED)
    ctx = tenant_ctx(conn)
    out = await service.receive(ctx, COM, {"amount": "35.00", "currency": "EUR", "received_on": "2026-10-30",
                                           "reference": "BANK-1"})  # fmt: skip
    assert conn.sql(flat(service.RECEIVE_SQL)) == [(COM, Decimal("35.00"), "EUR", dt.date(2026, 10, 30), "BANK-1")]
    got = out["commission_received"]
    assert (got["amount"], got["short_amount"], got["fx_difference_base"]) == ("35.00", "5.00", "-5.40")
    assert got["fx"] == {"rate": "1.08", "rate_date": "2026-10-30", "source": "platform_daily",
                         "basis": "receipt_date", "stale_days": 0, "attribution": fx.ATTRIBUTION}  # fmt: skip
    assert ctx.audit_events[-1].action == "commission_receive"


@pytest.mark.asyncio
async def test_a_receipt_without_a_day_is_today_and_an_agency_rate_carries_no_attribution() -> None:
    conn = world("expected", **{**RECEIVED, "fx_source": "agency_override"})
    out = await service.receive(tenant_ctx(conn), COM, {"amount": "1", "currency": "USD"})
    assert conn.sql(flat(service.RECEIVE_SQL))[0][3:] == (TODAY, None)
    assert "attribution" not in out["commission_received"]["fx"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"amount": "0", "currency": "USD"}, "invalid_amount"),
        ({"amount": "1.005", "currency": "USD"}, "invalid_amount"),
        ({"amount": "abc", "currency": "USD"}, "invalid_amount"),
        ({"amount": "Infinity", "currency": "USD"}, "invalid_amount"),
        ({"amount": "1000000000000", "currency": "USD"}, "invalid_amount"),
        ({"amount": "1", "currency": "usd"}, "invalid_currency"),
        ({"amount": "1", "currency": "USD", "received_on": "2026-11-02"}, "invalid_received_on"),  # tomorrow
        ({"amount": "1", "currency": "USD", "received_on": "30/10/2026"}, "invalid_received_on"),
        ({"amount": "1", "currency": "USD", "reference": "a\tb"}, "invalid_reference"),
    ],
)
async def test_a_bad_receipt_is_refused_before_writing(body: dict[str, Any], code: str) -> None:
    conn = world("expected")
    with pytest.raises(Invalid) as err:
        await service.receive(tenant_ctx(conn), COM, body)
    assert err.value.code == code
    assert conn.sql(flat(service.RECEIVE_SQL)) == []


class _FxMissing(asyncpg.PostgresError):
    sqlstate = "CRM25"


@pytest.mark.asyncio
async def test_a_receipt_without_a_rate_is_a_conflict_and_other_database_errors_pass() -> None:
    conn = world("expected").on(flat(service.RECEIVE_SQL), _FxMissing("no rate"))
    with pytest.raises(Conflict) as err:
        await service.receive(tenant_ctx(conn), COM, {"amount": "1", "currency": "THB", "received_on": "2026-10-01"})
    assert err.value.code == "fx_rate_missing" and "THB" in err.value.message and err.value.extra == {"currency": "THB"}
    assert conn.sql(flat(service.SYNC_SQL)) == []
    conn = world("expected").on(flat(service.RECEIVE_SQL), asyncpg.CheckViolationError("other"))
    with pytest.raises(asyncpg.CheckViolationError):
        await service.receive(tenant_ctx(conn), COM, {"amount": "1", "currency": "THB"})


@pytest.mark.asyncio
async def test_a_closed_commission_cannot_be_received() -> None:
    conn = world("written_off")
    with pytest.raises(Conflict) as err:
        await service.receive(tenant_ctx(conn), COM, {"amount": "1", "currency": "USD"})
    assert err.value.code == "commission_closed"


# ── write off / due date ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_write_off_keeps_the_reason_and_a_trimmed_note() -> None:
    conn = world("claimed", status="written_off", close_reason="too_small", note="Rounding")
    ctx = tenant_ctx(conn)
    out = await service.write_off(ctx, COM, "too_small", "  Rounding ")
    assert conn.sql(flat(service.WRITE_OFF_SQL)) == [(COM, "too_small", "Rounding", ACTOR)]
    assert out["close_reason"] == "too_small"
    assert (ctx.audit_events[-1].action, ctx.audit_events[-1].reason) == ("commission_write_off", "too_small")
    for note in (None, "   "):
        conn = world("expected")
        await service.write_off(tenant_ctx(conn), COM, "other", note)
        assert conn.sql(flat(service.WRITE_OFF_SQL))[0][2] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "note", "code"),
    [("lost_it", None, "invalid_reason"), ("other", "x" * 501, "invalid_note"), ("other", "a\nb", "invalid_note"),
     ("other", 7, "invalid_note")],
)  # fmt: skip
async def test_a_bad_write_off_is_refused_before_the_lock(reason: str, note: Any, code: str) -> None:
    conn = world("expected")
    with pytest.raises(Invalid) as err:
        await service.write_off(tenant_ctx(conn), COM, reason, note)
    assert err.value.code == code
    assert conn.calls == []


@pytest.mark.asyncio
async def test_a_new_due_date_clears_the_overdue_alert() -> None:
    conn = world("claimed", due_on=dt.date(2027, 1, 10))
    ctx = tenant_ctx(conn)
    out = await service.set_due(ctx, COM, "2027-01-10")
    assert conn.sql(flat(service.DUE_SQL)) == [(COM, dt.date(2027, 1, 10))]
    assert out["due_on"] == "2027-01-10" and (ctx.audit_events[-1].action, ctx.audit_events[-1].reason) == (
        "update", "due_on",
    )  # fmt: skip
    conn = world("claimed")
    await service.set_due(tenant_ctx(conn), COM, dt.date(2027, 2, 1))  # a date object passes as is
    assert conn.sql(flat(service.DUE_SQL)) == [(COM, dt.date(2027, 2, 1))]
    for bad_conn, value, exc in ((world("claimed"), "soon", Invalid), (world("received"), "2027-01-10", Conflict)):
        with pytest.raises(exc):
            await service.set_due(tenant_ctx(bad_conn), COM, value)
        assert bad_conn.sql(flat(service.DUE_SQL)) == []


# ── reads ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reads_one_commission_and_audits_the_view() -> None:
    conn = world("expected", due_on=dt.date(2026, 10, 20))
    ctx = tenant_ctx(conn)
    out = await service.get(ctx, COM)
    assert out["overdue"] and out["days_overdue"] == 12  # 20 Oct -> 1 Nov, the agency's today
    assert out["commission_expected"] == {"amount": "40.00", "currency": "EUR", "amount_base": "43.20",
                                          "base_currency": "USD"}  # fmt: skip
    assert [(e.action, e.object_id) for e in ctx.audit_events] == [("view", str(COM))]
    empty = ScriptedConn().on(flat(fx.TODAY_SQL), TODAY)
    with pytest.raises(NotFound):
        await service.get(tenant_ctx(empty), COM)


TOTALS = {
    "open_count": 3, "overdue_count": 1, "open_base": Decimal("120.5"), "overdue_base": Decimal("40"),
    "received_base": Decimal("37.8"), "received_expected_base": Decimal("43.2"), "open_other_base": 1,
}  # fmt: skip


def listing_world(rows: list[dict[str, Any]]) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(flat(fx.TODAY_SQL), TODAY)
    conn.on(flat(service.LIST_SQL), rows)
    conn.on(flat(service.TOTALS_SQL), TOTALS)
    conn.on(flat(fx.SETTINGS_SQL), {"base_currency": "VND", "booking_validation_level": "basic",
                                    "fx_mode": "automatic"})  # fmt: skip
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "statuses", "overdue_only"),
    [
        (None, ["expected", "claimed"], False),  # the default: open
        ("overdue", ["expected", "claimed"], True),
        ("received", ["received"], False),
        ("written_off", ["written_off"], False),
        ("cancelled", ["cancelled"], False),
        ("all", None, False),
    ],
)
async def test_the_list_passes_its_filter_and_reports_totals_in_the_base_currency(
    status: str | None, statuses: list[str] | None, overdue_only: bool
) -> None:
    conn = listing_world([row(), row(commission_id=uuid.UUID(int=2), due_on=dt.date(2026, 10, 1))])
    ctx = tenant_ctx(conn)
    out = await service.listing(ctx, status=status, trip_id=TRIP)
    assert conn.sql(flat(service.LIST_SQL)) == [(TRIP, statuses, overdue_only, TODAY, service.MAX_LIST)]
    assert conn.sql(flat(service.TOTALS_SQL)) == [("VND", TODAY, TRIP)]
    assert out["status"] == (status or "open") and out["today"] == "2026-11-01" and out["base_currency"] == "VND"
    assert [c["overdue"] for c in out["commissions"]] == [False, True]
    assert out["commission_totals"] == {"open_count": 3, "overdue_count": 1, "open_base": "120.50",
                                        "overdue_base": "40.00", "received_base": "37.80",
                                        "received_expected_base": "43.20", "open_other_base": 1}  # fmt: skip
    (event,) = ctx.audit_events
    assert event.action == "list_view" and event.object_ids == (str(COM), str(uuid.UUID(int=2)))


@pytest.mark.asyncio
async def test_an_unknown_list_filter_is_refused() -> None:
    conn = listing_world([])
    with pytest.raises(Invalid) as err:
        await service.listing(tenant_ctx(conn), status="late", trip_id=None)
    assert err.value.code == "invalid_status" and conn.calls == []


@pytest.mark.asyncio
async def test_the_bookings_tab_gets_the_live_row_of_each_line() -> None:
    other = uuid.UUID(int=9)
    rows = [
        row(status="cancelled", commission_id=uuid.UUID(int=1)),  # an old row of the line...
        row(status="expected", due_on=dt.date(2026, 10, 1)),  # ...replaced by the live one
        row(status="cancelled", commission_id=uuid.UUID(int=3)),  # a cancelled row after a live one never wins
        row(status="cancelled", booking_line_id=other, commission_id=uuid.UUID(int=4)),  # only a cancelled row
    ]
    conn = listing_world(rows)
    out = await service.for_lines(tenant_ctx(conn), TRIP)
    assert conn.sql(flat(service.LIST_SQL)) == [(TRIP, None, False, None, service.MAX_LIST)]
    assert out[LINE] == {"commission_id": str(COM), "status": "expected", "due_on": "2026-10-01", "overdue": True}
    assert out[other]["status"] == "cancelled"
    empty = listing_world([])
    assert await service.for_lines(tenant_ctx(empty), TRIP) == {}
    assert empty.sql(flat(fx.TODAY_SQL)) == []  # no row: not even today is read


# ── the date rule ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_reads_the_counts_as_text_or_json() -> None:
    conn = ScriptedConn().on(flat(service.SYNC_SQL), '{"created": 1, "cancelled": 0}')
    assert await service.sync(system_ctx(conn), TRIP) == {"created": 1, "cancelled": 0}
    conn = ScriptedConn().on(flat(service.SYNC_SQL), {"created": 2})
    assert await service.sync(system_ctx(conn), TRIP) == {"created": 2}
    assert await service.sync(system_ctx(ScriptedConn()), TRIP) == {}


@pytest.mark.asyncio
async def test_the_sweep_syncs_every_trip_it_finds() -> None:
    conn = ScriptedConn()
    conn.on(flat(service.SWEEP_SQL), [{"trip_id": TRIP}, {"trip_id": TRIP2}])
    conn.on(flat(service.SYNC_SQL), "{}")
    assert await service.sweep(system_ctx(conn)) == 2
    assert conn.sql(flat(service.SWEEP_SQL)) == [(service.MAX_SWEEP,)]
    assert conn.sql(flat(service.SYNC_SQL)) == [(TRIP,), (TRIP2,)]
    assert await service.sweep(system_ctx(ScriptedConn())) == 0


@pytest.mark.asyncio
async def test_overdue_rows_open_one_owner_task_per_trip_and_are_alerted_once() -> None:
    owner = uuid.UUID(int=77)
    a, b, c = uuid.UUID(int=11), uuid.UUID(int=12), uuid.UUID(int=13)
    conn = ScriptedConn()
    conn.on(flat(service.OVERDUE_SQL), [{"commission_id": a, "trip_id": TRIP}, {"commission_id": b, "trip_id": TRIP},
                                        {"commission_id": c, "trip_id": TRIP2}])  # fmt: skip
    conn.on(flat(service.OWNER_SQL), owner)
    assert await service.alert_overdue(system_ctx(conn), TODAY) == 3
    assert conn.sql(flat(service.OVERDUE_SQL)) == [(TODAY, service.MAX_SWEEP)]
    assert conn.sql(flat(service.TASK_SQL)) == [(TRIP, service.OVERDUE_TASK, owner), (TRIP2, service.OVERDUE_TASK,
                                                                                       owner)]  # fmt: skip
    assert conn.sql(flat(service.ALERTED_SQL)) == [([a, b, c],)]
    quiet = ScriptedConn()
    assert await service.alert_overdue(system_ctx(quiet), TODAY) == 0
    assert quiet.sql(flat(service.OWNER_SQL)) == [] and quiet.sql(flat(service.ALERTED_SQL)) == []


@pytest.mark.asyncio
async def test_supplier_tasks_close_with_the_actor() -> None:
    conn = ScriptedConn().on(flat(service.CLOSE_SUPPLIER_TASKS_SQL), [{"task_id": uuid.UUID(int=1)}])
    assert await service.close_supplier_tasks(system_ctx(conn), "job:crm_stages") == 1
    assert conn.sql(flat(service.CLOSE_SUPPLIER_TASKS_SQL)) == [(SUPPLIER_TASK, "job:crm_stages")]
