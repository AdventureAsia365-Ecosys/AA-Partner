"""AA-1144: the closed list of agency items (app.ledger.agency_items) and the B11 request to AA for extra nights
(app.booking.extra_nights), without a database (the integration tests hold the SQL: test_aa1144_agency_items_api.py,
test_crm080_schema.py)."""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.booking import extra_nights
from app.errors import Conflict, Invalid, NotFound
from app.ledger import agency_items
from app.observability import ops_alerts
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

TRIP = uuid.UUID(int=41)
REQUEST = uuid.UUID(int=42)
BLOCK = uuid.UUID(int=43)
CHECK_IN = dt.date(2027, 3, 8)
BODY = {"position": "after", "place": "Lantern Bay", "check_in": CHECK_IN.isoformat(), "nights": 2, "rooms": 1,
        "adults": 2}  # fmt: skip


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "request_id": REQUEST, "trip_id": TRIP, "position": "after", "place": "Lantern Bay", "check_in": CHECK_IN,
        "nights": 2, "rooms": 1, "adults": 2, "children": 0, "status": "open", "close_reason": None,
        "closed_at": None, "created_at": dt.datetime(2026, 11, 19, 8, tzinfo=dt.UTC),
    }  # fmt: skip
    row.update(over)
    return row


# ── the closed list ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["flight", "visa", "insurance", "fee"])
def test_the_four_agency_items_pass(value: str) -> None:
    assert agency_items.category(value) == value


@pytest.mark.parametrize("value", ["transfer", "other", "hotel", "accommodation", "activity", "guide", "", None, 3])
def test_anything_else_is_refused_with_the_reason(value: Any) -> None:
    with pytest.raises(Invalid) as err:
        agency_items.category(value)
    assert err.value.code == "invalid_category" and err.value.extra["allowed"] == ["flight", "visa", "insurance", "fee"]
    assert "Adventure Asia" in err.value.message


@pytest.mark.parametrize("value", ["accommodation", "transport", "activity", "guide", "restaurant", "dmc", "hotel"])
def test_an_own_supplier_of_a_catalogue_type_is_refused(value: str) -> None:
    with pytest.raises(Invalid) as err:
        agency_items.supplier_category(value)
    assert err.value.code == "invalid_category"


@pytest.mark.parametrize("value", ["flight", "insurance", "visa", "other"])
def test_an_own_supplier_of_the_list_passes(value: str) -> None:
    assert agency_items.supplier_category(value) == value


@pytest.mark.parametrize("kind", ["lodging", "activity", "transport", "meal", "guide"])
def test_a_product_block_takes_aa_items_only(kind: str) -> None:
    with pytest.raises(Invalid) as err:
        agency_items.block_takes_agency_items(kind)
    assert err.value.code == "agency_item_not_allowed" and err.value.extra["block_type"] == kind
    agency_items.block_takes_agency_items("other")


def test_a_block_with_agency_items_keeps_its_type() -> None:
    block = {"option_block_id": BLOCK, "type": "other"}
    agency = [{"option_block_id": BLOCK, "source": "agency"}]
    aa_only = [{"option_block_id": BLOCK, "source": "aa"}, {"option_block_id": uuid.uuid4(), "source": "agency"}]
    agency_items.block_type_change("other", block, agency)  # unchanged
    agency_items.block_type_change("lodging", block, aa_only)  # no agency item on it
    with pytest.raises(Invalid) as err:
        agency_items.block_type_change("lodging", block, agency)
    assert err.value.code == "agency_item_not_allowed"


# ── B11: ask AA for extra nights ─────────────────────────────────────────────────────────────────


def _conn(*, trip: bool = True, closed: bool = False, row: dict[str, Any] | None = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("SELECT trip_id FROM crm.trip", TRIP if trip else None)
    conn.on("SELECT t.stage", {"stage": "cancelled" if closed else "proposal", "cancelling": False})
    conn.on("INSERT INTO crm.aa_extra_night_request", REQUEST)
    conn.on("FROM crm.aa_extra_night_request WHERE request_id", row if row is not None else _row())
    return conn


@pytest.mark.asyncio
async def test_a_request_is_stored_audited_and_raises_the_ops_check() -> None:
    ops_alerts.PENDING.drain()
    conn = _conn()
    ctx = tenant_ctx(conn, role="assistant")
    out = await extra_nights.create(ctx, TRIP, BODY)
    assert conn.sql("INSERT INTO crm.aa_extra_night_request")[0] == (TRIP, "after", "Lantern Bay", CHECK_IN, 2, 1, 2, 0)
    assert out["check_out"] == "2027-03-10" and out["status"] == "open"
    assert ctx.audit_events[0].action == "create"
    raised = ops_alerts.PENDING.drain()
    assert [f.check for f in raised] == ["aa_extra_nights_requested"] and raised[0].scope == str(REQUEST)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"position": "during"}, "invalid_position"),
        ({"place": ""}, "invalid_place"),
        ({"place": "x" * 81}, "invalid_place"),
        ({"place": "Bay\nside"}, "invalid_place"),
        ({"check_in": "8 March"}, "invalid_check_in"),
        ({"nights": 0}, "invalid_nights"),
        ({"nights": 61}, "invalid_nights"),
        ({"nights": True}, "invalid_nights"),
        ({"rooms": 31}, "invalid_rooms"),
        ({"adults": 0}, "invalid_adults"),
        ({"children": -1}, "invalid_children"),
    ],
)
async def test_a_request_outside_the_bounds_is_refused(change: dict[str, Any], code: str) -> None:
    conn = _conn()
    with pytest.raises(Invalid) as err:
        await extra_nights.create(tenant_ctx(conn), TRIP, {**BODY, **change})
    assert err.value.code == code and not conn.sql("INSERT INTO crm.aa_extra_night_request")


@pytest.mark.asyncio
async def test_a_missing_or_cancelled_trip_asks_nothing() -> None:
    with pytest.raises(NotFound):
        await extra_nights.create(tenant_ctx(_conn(trip=False)), TRIP, BODY)
    with pytest.raises(NotFound):
        await extra_nights.list_for_trip(tenant_ctx(_conn(trip=False)), TRIP)
    with pytest.raises(Conflict) as err:
        await extra_nights.create(tenant_ctx(_conn(closed=True)), TRIP, BODY)
    assert err.value.code == "trip_cancelled"


@pytest.mark.asyncio
async def test_the_trip_list_and_the_close() -> None:
    conn = _conn().on("WHERE trip_id = $1 AND deleted_at IS NULL", [_row()])
    listed = await extra_nights.list_for_trip(tenant_ctx(conn), TRIP)
    assert [r["request_id"] for r in listed["requests"]] == [str(REQUEST)]
    with pytest.raises(Invalid):
        await extra_nights.close(tenant_ctx(_conn()), REQUEST, "maybe")
    conn = _conn().on("UPDATE crm.aa_extra_night_request", REQUEST)
    ctx = tenant_ctx(conn)
    await extra_nights.close(ctx, REQUEST, "quoted")
    assert conn.sql("UPDATE crm.aa_extra_night_request")[0] == (REQUEST, "quoted")
    assert ctx.audit_events[0].reason == "quoted"
    with pytest.raises(Conflict) as err:  # closed already: the guarded UPDATE matched nothing
        await extra_nights.close(tenant_ctx(_conn()), REQUEST, "not_needed")
    assert err.value.code == "request_closed"
    gone = ScriptedConn().on("FROM crm.aa_extra_night_request WHERE request_id", None)
    with pytest.raises(NotFound):
        await extra_nights.close(tenant_ctx(gone), REQUEST, "quoted")


class _OpsCtx:
    def __init__(self, tenant: uuid.UUID, conn: ScriptedConn) -> None:
        self.tenant_id, self.conn, self.audited = tenant, conn, []

    def audit(self, action: str, *_args: Any, **kw: Any) -> None:
        self.audited.append((action, kw.get("object_ids")))


@pytest.mark.asyncio
async def test_the_staff_board_lists_the_open_requests_of_every_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    """AA ground operations: one agency at a time, the agency name and a trip reference, nothing personal; an agency
    the context refuses (offboarding, or not synthetic for the Dev E2E identity) is skipped."""
    first, second, gone = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    rows = {
        first: [_row(check_in=dt.date(2027, 4, 1))],
        second: [_row(request_id=uuid.UUID(int=52), trip_id=uuid.UUID(int=62), check_in=dt.date(2027, 3, 1))],
    }
    contexts: list[_OpsCtx] = []

    class _Agency:
        def __init__(self, tenant_id: uuid.UUID) -> None:
            self.tenant_id = tenant_id

    async def agencies(_pool: Any) -> list[_Agency]:
        return [_Agency(first), _Agency(second), _Agency(gone)]

    @asynccontextmanager
    async def ops_tx(_pool: Any, _caller: Any, tenant: uuid.UUID):
        if tenant == gone:
            raise NotFound("agency_not_found")
        conn = ScriptedConn().on("WHERE status = 'open'", rows[tenant]).on("display_name", f"Agency {tenant.int}")
        ctx = _OpsCtx(tenant, conn)
        contexts.append(ctx)
        yield ctx

    monkeypatch.setattr(extra_nights.registry, "agencies", agencies)
    monkeypatch.setattr(extra_nights, "ops_tx", ops_tx)
    board = await extra_nights.staff_board(object(), object())  # type: ignore[arg-type]
    assert [(i["agency_name"], i["check_in"]) for i in board["items"]] == [
        ("Agency 2", "2027-03-01"),
        ("Agency 1", "2027-04-01"),
    ]
    assert board["items"][0]["trip_ref"] == str(uuid.UUID(int=62))[:8]
    assert [c.audited[0][0] for c in contexts] == ["extra_nights_list", "extra_nights_list"]


@pytest.mark.asyncio
async def test_a_date_object_is_taken_as_is() -> None:
    conn = _conn()
    await extra_nights.create(tenant_ctx(conn), TRIP, {**BODY, "check_in": CHECK_IN})
    assert conn.sql("INSERT INTO crm.aa_extra_night_request")[0][3] == CHECK_IN
