"""AA-1069 (crm-052 / crm-053) without a database: the service catalogue and the supplier booking requests.

- Quantity per basis, the tier of a party size, the party read from a room list or the trip party, the option title.
- Catalogue validation (category, basis, amounts, tiers that overlap) and the views (no net for the assistant).
- "Add from catalogue": the snapshot body (sell / net x quantity, the tier, FX), inactive / out-of-window / no rate.
- Supplier requests: the request text (EN / VI, names only when given), the build grouping per supplier, a supplier
  without an e-mail, send marks the lines, back to planning, the route models.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from pydantic import ValidationError

from app.catalogue import service as catalogue
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ledger import requests
from app.routers import catalogue as catalogue_routes
from app.routers import ledger as ledger_routes
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

SERVICE = uuid.uuid4()
SUPPLIER = uuid.uuid4()
TRIP = uuid.uuid4()
DAY = dt.date(2027, 3, 10)
NOW = dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.UTC)


def _service(**over: Any) -> dict[str, Any]:
    row = {
        "service_id": SERVICE,
        "agency_supplier_id": SUPPLIER,
        "supplier_name": "Lantern Air",
        "category": "flight",
        "name": "Valley hop flight",
        "unit_net": Decimal("90.00"),
        "unit_sell": Decimal("120.00"),
        "currency": "USD",
        "qty_basis": "per_person",
        "valid_from": None,
        "valid_to": None,
        "active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(over)
    return row


def _tiers() -> list[dict[str, Any]]:
    return [
        {"tier_id": uuid.uuid4(), "service_id": SERVICE, "min_pax": 1, "max_pax": 2, "unit_net": Decimal("95")},
        {"tier_id": uuid.uuid4(), "service_id": SERVICE, "min_pax": 3, "max_pax": None, "unit_net": Decimal("85")},
    ]


PROPOSAL = {"currency": "USD", "room_config": None, "party_adults": 2, "party_child_ages": [8], "trip_nights": None}


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        ("per_person", 3),
        ("per_adult", 2),
        ("per_booking", 1),
        ("per_room", 2),
        ("per_night", 4),
        ("per_person_night", 12),
    ],
)
def test_quantity_per_basis(basis: str, expected: int) -> None:
    assert catalogue.quantity(basis, {"travellers": 3, "adults": 2, "rooms": 2}, 4) == expected


def test_quantity_refuses_an_unknown_basis_and_nights_count_at_least_one() -> None:
    with pytest.raises(Invalid):
        catalogue.quantity("per_week", {"travellers": 1, "adults": 1, "rooms": 1}, 1)
    assert catalogue.quantity("per_night", {"travellers": 1, "adults": 1, "rooms": 1}, 0) == 1


def test_the_party_from_rooms_or_the_trip() -> None:
    assert catalogue.party(None, 2, [8, 1]) == {"travellers": 4, "adults": 2, "rooms": 1}
    rooms = [{"type": "double", "adults": 2, "children": 1}, {"type": "single", "adults": 1, "children": 0}]
    assert catalogue.party(rooms, 2, []) == {"travellers": 4, "adults": 3, "rooms": 2}


def test_the_tier_of_the_party_size() -> None:
    tiers = _tiers()
    assert catalogue.tier_net(Decimal("90"), tiers, 2) == Decimal("95")
    assert catalogue.tier_net(Decimal("90"), tiers, 7) == Decimal("85")
    assert catalogue.tier_net(Decimal("90"), [], 7) == Decimal("90")
    assert catalogue.tier_net(None, [], 7) is None


def test_the_option_title() -> None:
    assert catalogue.option_title("Visa letter", 1) == "Visa letter"
    assert catalogue.option_title("Visa letter", 3) == "Visa letter x 3"


def test_tiers_in_sorts_and_refuses_overlaps() -> None:
    out = catalogue.tiers_in(
        [{"min_pax": 5, "max_pax": None, "unit_net": "70"}, {"min_pax": 1, "max_pax": 4, "unit_net": "80"}]
    )
    assert [t[0] for t in out] == [1, 5]
    with pytest.raises(Conflict):
        catalogue.tiers_in([{"min_pax": 1, "max_pax": None, "unit_net": "1"}, {"min_pax": 3, "unit_net": "1"}])
    for bad in (
        [{"min_pax": 0, "unit_net": "1"}],
        [{"min_pax": 3, "max_pax": 2, "unit_net": "1"}],
        [{"min_pax": 1, "unit_net": "-1"}],
        ["x"],
        "x",
        [{"min_pax": True, "unit_net": "1"}],
    ):
        with pytest.raises(Invalid):
            catalogue.tiers_in(bad)


def test_unit_amounts() -> None:
    assert catalogue.unit("12.5", "unit_sell") == Decimal("12.50")
    assert catalogue.unit(None, "unit_net", optional=True) is None
    for bad in ("abc", "-1", "100000000", "NaN"):
        with pytest.raises(Invalid):
            catalogue.unit(bad, "unit_sell")


# ── catalogue reads and writes ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_assistant_reads_the_catalogue_without_the_net() -> None:
    conn = ScriptedConn().on("FROM crm.agency_service s", [_service()]).on("FROM crm.agency_service_tier", _tiers())
    owner = await catalogue.list_services(tenant_ctx(conn), category=None, active=None)
    assert owner["services"][0]["unit_net"] == "90.00" and len(owner["services"][0]["tiers"]) == 2
    assistant = await catalogue.list_services(tenant_ctx(conn, role="assistant"), category="flight", active=True)
    item = assistant["services"][0]
    assert "unit_net" not in item and "tiers" not in item and item["tier_count"] == 2
    with pytest.raises(Invalid):
        await catalogue.list_services(tenant_ctx(conn), category="hotel", active=None)


@pytest.mark.asyncio
async def test_get_and_delete_a_missing_service() -> None:
    with pytest.raises(NotFound):
        await catalogue.get_service(tenant_ctx(ScriptedConn()), SERVICE)
    with pytest.raises(NotFound):
        await catalogue.delete_service(tenant_ctx(ScriptedConn()), SERVICE)
    deleted = await catalogue.delete_service(
        tenant_ctx(ScriptedConn().on("DELETE FROM crm.agency_service", SERVICE)), SERVICE
    )
    assert deleted["status"] == "deleted"


@pytest.mark.asyncio
async def test_create_validates_and_writes_the_tiers() -> None:
    conn = (
        ScriptedConn()
        .on("SELECT kind, deleted_at FROM crm.agency_supplier", {"kind": "own", "deleted_at": None})
        .on("INSERT INTO crm.agency_service ", SERVICE)
        .on("FROM crm.agency_service s", _service())
    )
    body = {
        "agency_supplier_id": str(SUPPLIER),
        "category": "flight",
        "name": " Valley hop flight ",
        "unit_net": "90",
        "unit_sell": "120",
        "currency": "USD",
        "qty_basis": "per_person",
        "tiers": [{"min_pax": 1, "max_pax": 2, "unit_net": "95"}],
    }
    out = await catalogue.create_service(tenant_ctx(conn), body)
    assert out["service_id"] == str(SERVICE)
    assert conn.sql("INSERT INTO crm.agency_service_tier") == [(SERVICE, 1, 2, Decimal("95.00"))]
    assert conn.sql("INSERT INTO crm.tenant_settings")  # the settings row the service hangs off
    for bad in (
        {"category": "hotel"},
        {"qty_basis": "per_week"},
        {"name": ""},
        {"currency": "usd"},
        {"active": "yes"},
        {"valid_from": "2027-03-10", "valid_to": "2027-03-01"},
        {"valid_to": "x"},
    ):
        with pytest.raises(Invalid):
            await catalogue.create_service(tenant_ctx(conn), {**body, **bad})
    aa = ScriptedConn().on("SELECT kind, deleted_at FROM crm.agency_supplier", {"kind": "aa", "deleted_at": None})
    with pytest.raises(Invalid):
        await catalogue.create_service(tenant_ctx(aa), body)


@pytest.mark.asyncio
async def test_database_refusals_are_worded() -> None:
    def crm52(text: str) -> asyncpg.PostgresError:
        err = asyncpg.PostgresError(text)
        err.sqlstate = "CRM52"  # type: ignore[attr-defined]
        return err

    assert isinstance(catalogue._refusal(crm52("crm: catalogue_owner_agent (x)")), Forbidden)
    assert isinstance(catalogue._refusal(crm52("crm: catalogue_tier_overlap (x)")), Conflict)
    assert isinstance(catalogue._refusal(crm52("crm: catalogue_supplier (x)")), Invalid)
    assert isinstance(catalogue._refusal(asyncpg.CheckViolationError("x")), Invalid)
    assert isinstance(catalogue._refusal(asyncpg.ForeignKeyViolationError("x")), Invalid)
    other = asyncpg.PostgresError("other")
    assert catalogue._refusal(other) is other
    conn = ScriptedConn().on("INSERT INTO crm.agency_service ", crm52("crm: catalogue_owner_agent (role assistant)"))
    with pytest.raises(Forbidden):
        await catalogue.create_service(
            tenant_ctx(conn, role="assistant"),
            {"category": "fee", "name": "Fee", "unit_sell": "1", "currency": "USD", "qty_basis": "per_booking"},
        )


@pytest.mark.asyncio
async def test_update_changes_only_the_sent_fields() -> None:
    conn = ScriptedConn().on("FROM crm.agency_service s", _service())
    await catalogue.update_service(
        tenant_ctx(conn),
        SERVICE,
        {
            "name": "Hop flight",
            "unit_net": None,
            "active": False,
            "valid_to": "2027-12-31",
            "category": "flight",
            "unit_sell": "130",
            "currency": "EUR",
            "qty_basis": "per_adult",
            "valid_from": None,
            "agency_supplier_id": None,
            "tiers": [],
        },
    )
    (update,) = [flat for flat, _ in conn.calls if flat.startswith("UPDATE crm.agency_service SET")]
    assert "name = $" in update and "active = $" in update
    assert conn.sql("DELETE FROM crm.agency_service_tier")  # tiers [] replaces the list
    with pytest.raises(Invalid):
        await catalogue.update_service(tenant_ctx(conn), SERVICE, {"active": "no"})
    with pytest.raises(Invalid):  # the stored start is after the new end
        await catalogue.update_service(
            tenant_ctx(ScriptedConn().on("FROM crm.agency_service s", _service(valid_from=DAY))),
            SERVICE,
            {"valid_to": "2027-01-01"},
        )
    with pytest.raises(NotFound):
        await catalogue.update_service(tenant_ctx(ScriptedConn()), SERVICE, {"name": "x"})


# ── Add from catalogue ───────────────────────────────────────────────────────────────────────────


def _plan_conn(row: dict[str, Any], *, rate: Any = None) -> ScriptedConn:
    conn = ScriptedConn().on("FROM crm.agency_service s", row).on("FROM crm.agency_service_tier", _tiers())
    conn.on("FROM crm.tenant_settings", DAY)  # the agency's today (fx.agency_today)
    if rate is not None:
        conn.on("crm.fx_resolve", rate)
    return conn


@pytest.mark.asyncio
async def test_plan_option_is_a_snapshot_with_the_tier() -> None:
    body, summary = await catalogue.plan_option(
        tenant_ctx(_plan_conn(_service())),
        SERVICE,
        proposal=PROPOSAL,
        service_date=DAY,
        days=5,
        quantity_override=None,
        nights_override=None,
    )
    assert body == {
        "title": "Valley hop flight x 3",
        "agency_supplier_id": SUPPLIER,
        "sell_amount": Decimal("360.00"),
        "net_amount": Decimal("255.00"),
    }
    assert (summary["quantity"], summary["unit_net"], summary["nights"]) == (3, "85", 4)
    _, hidden = await catalogue.plan_option(
        tenant_ctx(_plan_conn(_service()), role="assistant"),
        SERVICE,
        proposal=PROPOSAL,
        service_date=DAY,
        days=5,
        quantity_override=2,
        nights_override=None,
    )
    assert "unit_net" not in hidden and "net_amount" not in hidden and hidden["quantity"] == 2


@pytest.mark.asyncio
async def test_plan_option_converts_another_currency() -> None:
    rate = {"rate": Decimal("1.1"), "rate_date": DAY, "fx_source": "manual", "stale_days": 0}
    body, summary = await catalogue.plan_option(
        tenant_ctx(_plan_conn(_service(currency="EUR", unit_net=None, qty_basis="per_booking"), rate=rate)),
        SERVICE,
        proposal=PROPOSAL,
        service_date=None,
        days=1,
        quantity_override=None,
        nights_override=None,
    )
    assert body["sell_amount"] == Decimal("132.00") and body["net_amount"] == Decimal("85.00") * Decimal("1.1")
    assert summary["fx"]["rate"] == "1.1"


@pytest.mark.asyncio
async def test_plan_option_refusals() -> None:
    async def plan(row: dict[str, Any] | None, **kw: Any) -> Any:
        conn = _plan_conn(row) if row is not None else ScriptedConn()
        args = {
            "proposal": PROPOSAL,
            "service_date": DAY,
            "days": 3,
            "quantity_override": None,
            "nights_override": None,
            **kw,
        }
        return await catalogue.plan_option(tenant_ctx(conn), SERVICE, **args)

    with pytest.raises(NotFound):
        await plan(None)
    for row, code in (
        (_service(active=False), "service_inactive"),
        (_service(valid_to=DAY - dt.timedelta(days=1)), "service_not_valid"),
        (_service(valid_from=DAY + dt.timedelta(days=1)), "service_not_valid"),
        (_service(currency="EUR"), "fx_rate_missing"),
    ):
        with pytest.raises(Conflict) as err:
            await plan(row)
        assert err.value.code == code
    with pytest.raises(Invalid):
        await plan(_service(), quantity_override=0)


# ── supplier requests ────────────────────────────────────────────────────────────────────────────


TRIP_ROW = {
    "trip_id": TRIP,
    "title": "Hill towns 6 days",
    "start_date": DAY,
    "end_date": DAY + dt.timedelta(days=5),
    "nights": 5,
    "party_adults": 2,
    "party_child_ages": [8],
    "agency_name": "Sunrise Journeys",
    "default_locale": "en-AU",
}


def _line(
    supplier: uuid.UUID,
    title: str,
    *,
    status: str = "planning",
    has_email: bool = True,
    category: str = "flight",
    name: str = "Lantern Air",
) -> dict[str, Any]:
    return {
        "booking_line_id": uuid.uuid4(),
        "agency_supplier_id": supplier,
        "title": title,
        "service_from": DAY,
        "service_to": DAY + dt.timedelta(days=1),
        "status": status,
        "supplier_name": name,
        "category": category,
        "has_email": has_email,
    }


def test_the_request_text_in_english_and_vietnamese() -> None:
    lines = [{"title": "Flight to the hills", "service_from": DAY, "service_to": DAY}]
    rooms = [{"type": "double"}, {"type": "double"}, {"type": "single"}]
    subject, body = requests.compose(
        "en", agency="Sunrise Journeys", supplier="Lantern Air", trip=TRIP_ROW, lines=lines, rooms=rooms, names=None
    )
    assert subject == "Booking request: Hill towns 6 days (10 Mar 2027)"
    assert "Travellers: 2 adults, 1 child (ages 8)" in body and "Rooms: 2 double, 1 single" in body
    assert "1. Flight to the hills - 10 Mar 2027" in body and "names" not in body.lower()
    assert body.endswith("Sunrise Journeys")
    _, vi = requests.compose(
        "vi",
        agency="Sunrise Journeys",
        supplier="Lantern Air",
        trip={**TRIP_ROW, "start_date": None},
        lines=lines,
        rooms=None,
        names=["Mai Tran", "Long Vo"],
    )
    assert "Ngày đi: sẽ xác nhận sau" in vi and "- Mai Tran" in vi and "Phòng" not in vi
    assert requests.party_text("en", 1, []) == "1 adult" and requests.party_text("en", 0, []) == "-"
    assert requests._lang("vi-VN") == "vi" and requests._lang(None) == "en"


@pytest.mark.asyncio
async def test_build_groups_lines_per_supplier() -> None:
    other = uuid.uuid4()
    rows = [
        _line(SUPPLIER, "Flight to the hills"),
        _line(SUPPLIER, "Flight back"),
        _line(other, "Boat transfer", has_email=False, category="transport", name="River Boats"),
        _line(SUPPLIER, "Old flight", status="confirmed"),
    ]
    conn = (
        ScriptedConn()
        .on("CROSS JOIN crm.tenant", TRIP_ROW)
        .on("FROM crm.booking_line l JOIN", rows)
        .on(
            "FROM crm.trip_traveler tt JOIN crm.traveler",
            [{"traveler_id": uuid.uuid4(), "display_name_enc": b"enc:traveler.x:Mai Tran"}],
        )
        .on("SELECT conversation_id FROM crm.conversation", uuid.uuid4())
        .on(
            "wa_window_expires_at",
            {
                "conversation_id": uuid.uuid4(),
                "trip_id": TRIP,
                "traveler_id": None,
                "channel": "email",
                "agency_supplier_id": SUPPLIER,
            },
        )
        .on("FOR UPDATE", None)
        .on("INSERT INTO crm.message", NOW)
    )
    out = await requests.build(tenant_ctx(conn), FakeVault(), TRIP)  # type: ignore[arg-type]
    by_name = {r["supplier_name"]: r for r in out["requests"]}
    assert len(by_name["Lantern Air"]["line_ids"]) == 2 and by_name["Lantern Air"]["include_names"] is True
    assert "- Mai Tran" in by_name["Lantern Air"]["body"]
    assert by_name["River Boats"]["reason"] == "supplier_no_email" and "message_id" not in by_name["River Boats"]
    with pytest.raises(Conflict):  # an unknown or confirmed line only: nothing to request
        await requests.build(tenant_ctx(conn), FakeVault(), TRIP, line_ids=[uuid.uuid4(), rows[3]["booking_line_id"]])  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await requests.build(tenant_ctx(ScriptedConn()), FakeVault(), TRIP)  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await requests.build(tenant_ctx(conn), FakeVault(), TRIP, line_ids=["x"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_marks_only_the_lines_of_that_supplier(monkeypatch: pytest.MonkeyPatch) -> None:
    message = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()

    async def fake_send(*_: Any, **__: Any) -> dict[str, Any]:
        return {
            "message_id": str(message),
            "status": "queued",
            "dispatch_id": "d",
            "undo_until": "u",
            "conversation_id": "c",
        }

    monkeypatch.setattr(requests.conversations, "send", fake_send)
    row = {
        "message_id": message,
        "status": "draft_autosaved",
        "author_member_id": None,
        "conversation_id": uuid.uuid4(),
        "trip_id": TRIP,
        "agency_supplier_id": SUPPLIER,
    }
    conn = (
        ScriptedConn().on("JOIN crm.conversation c", row).on("SET status = 'requested'", [{"booking_line_id": first}])
    )
    out = await requests.send(
        tenant_ctx(conn), FakeVault(), TRIP, message_id=message, subject="s", body="b", line_ids=[first, second]
    )  # type: ignore[arg-type]
    assert out["requested"] == [str(first)] and out["not_requested"] == [str(second)]
    (args,) = conn.sql("SET status = 'requested'")
    assert args[2] == SUPPLIER and args[3] == str(message)
    with pytest.raises(Conflict):
        await requests.send(
            tenant_ctx(ScriptedConn().on("JOIN crm.conversation c", row)),
            FakeVault(),
            TRIP,
            message_id=message,
            subject="s",
            body="b",
            line_ids=[first],
        )  # type: ignore[arg-type]
    with pytest.raises(NotFound):  # a client conversation's message is not a supplier request
        await requests.send(
            tenant_ctx(ScriptedConn().on("JOIN crm.conversation c", {**row, "agency_supplier_id": None})),
            FakeVault(),
            TRIP,
            message_id=message,
            subject="s",
            body="b",
            line_ids=[first],
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_back_to_planning() -> None:
    line = uuid.uuid4()
    conn = (
        ScriptedConn()
        .on(
            "SELECT trip_id, status, supplier_kind", {"trip_id": TRIP, "status": "requested", "supplier_kind": "agency"}
        )
        .on("SET status = 'planning'", TRIP)
    )
    assert await requests.back_to_planning(tenant_ctx(conn), line) == TRIP
    for current in (
        {"trip_id": TRIP, "status": "planning", "supplier_kind": "agency"},
        {"trip_id": TRIP, "status": "requested", "supplier_kind": "aa"},
    ):
        with pytest.raises(Conflict):
            await requests.back_to_planning(tenant_ctx(ScriptedConn().on("SELECT trip_id, status", current)), line)
    with pytest.raises(NotFound):
        await requests.back_to_planning(tenant_ctx(ScriptedConn()), line)


# ── route models ─────────────────────────────────────────────────────────────────────────────────


def test_the_route_models() -> None:
    made = catalogue_routes.ServiceIn(
        category="visa",
        name="Visa letter",
        unit_sell=Decimal("30"),
        currency="USD",
        qty_basis="per_person",
        tiers=[{"min_pax": 1, "unit_net": Decimal("20")}],
    )
    assert catalogue_routes._body(made)["tiers"] == [{"min_pax": 1, "unit_net": Decimal("20")}]
    change = catalogue_routes.ServiceChange(name=None, unit_net=None)
    assert catalogue_routes._body(change) == {"unit_net": None}  # a null name keeps it; a null net clears it
    with pytest.raises(ValidationError):
        catalogue_routes.ServiceIn(
            category="hotel", name="x", unit_sell=Decimal("1"), currency="USD", qty_basis="per_person"
        )
    with pytest.raises(ValidationError):
        ledger_routes.SupplierRequestSendIn(message_id=uuid.uuid4(), body="", line_ids=[uuid.uuid4()])
    assert ledger_routes.ConfirmIn().confirmation_no is None
    assert ledger_routes.SupplierRequestsIn().include_names is None
