"""AA-1070 without a database: the travel ops routes reach their helpers (and refuse who they must), the pure checks
of the passport rule, the rooming list, the vouchers, the visa tracker and the departure roster, the PDF writer and
its documents, the PDF job and the portal wrappers. The SQL runs on PostgreSQL in
``tests/integration/test_aa1070_travel_ops_db.py``. Invented names and places only."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.documents import pdf, pdf_job, renders
from app.errors import Conflict, Forbidden, Invalid, NotFound, RateLimited
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.main import create_app
from app.observability import reveal_meter
from app.portal import public as portal
from app.routers import travel_ops as router
from app.travel_ops import departures, passport, rooming, visas, vouchers
from app.vault import DecryptionDenied
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, enc, fake_tx, system_ctx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000010a0"
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000010a1")
LEAD = uuid.UUID("00000000-0000-4000-8000-0000000010b1")
MATE = uuid.UUID("00000000-0000-4000-8000-0000000010b2")
LINE = uuid.UUID("00000000-0000-4000-8000-0000000010c1")
APP = uuid.UUID("00000000-0000-4000-8000-0000000010d1")
END = dt.date(2031, 3, 31)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Refusal(asyncpg.PostgresError):
    """A database refusal with a SQLSTATE (the hold guard's CRM10)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("refused")
        self.sqlstate = sqlstate


# ── routes ───────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []

    def stub(name: str) -> Any:
        async def fn(*_: Any, **__: Any) -> dict[str, Any]:
            out.append(name)
            return {"name": name}

        return fn

    for module, names in (
        (departures, ("roster_list", "roster", "manifest")),
        (passport, ("trip_view", "settings_view", "update_settings")),
        (rooming, ("view", "replace")),
        (vouchers, ("trip_vouchers", "get_details", "save_details")),
        (pdf_job, ("request",)),
        (visas, ("trip_applications", "suggest", "create", "get", "update", "delete", "add_item", "set_item",
                 "delete_item", "list_templates", "create_template", "update_template", "delete_template")),
    ):  # fmt: skip
        for name in names:
            monkeypatch.setattr(module, name, stub(name))

    async def today(_ctx: Any) -> dt.date:
        return dt.date(2031, 1, 1)

    monkeypatch.setattr(router.fx, "agency_today", today)
    return out


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn().on("SELECT end_date", END), role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


ROOMS = {"rooms": [{"room_no": 1, "room_type": "twin", "travellers": [{"traveler_id": ID}]}]}
TEMPLATE = {"country": "VN", "visa_type": "evisa", "documents": ["Photo"]}
REQUESTS = [
    ("GET", "/crm/v1/departures?scope=all", None, "roster_list"),
    ("GET", "/crm/v1/departures/DEP-1/roster", None, "roster"),
    ("POST", "/crm/v1/departures/DEP-1/manifest", {"fields": ["name"]}, "manifest"),
    ("GET", f"/crm/v1/trips/{ID}/passports", None, "trip_view"),
    ("GET", f"/crm/v1/trips/{ID}/rooming", None, "view"),
    ("PUT", f"/crm/v1/trips/{ID}/rooming", ROOMS, "replace"),
    ("GET", f"/crm/v1/trips/{ID}/vouchers", None, "trip_vouchers"),
    ("GET", f"/crm/v1/lines/{ID}/voucher", None, "get_details"),
    ("PUT", f"/crm/v1/lines/{ID}/voucher", {"pickup": "Pier 4", "service_time": "07:30"}, "save_details"),
    ("POST", f"/crm/v1/trips/{ID}/pdfs", {"kind": "voucher", "booking_line_id": ID}, "request"),
    ("GET", f"/crm/v1/trips/{ID}/visas", None, "trip_applications"),
    ("GET", f"/crm/v1/trips/{ID}/visas/suggestions", None, "suggest"),
    ("POST", f"/crm/v1/trips/{ID}/visas", {"traveler_id": ID, "country": "VN"}, "create"),
    ("GET", f"/crm/v1/visas/{ID}", None, "get"),
    ("PATCH", f"/crm/v1/visas/{ID}", {"status": "collecting"}, "update"),
    ("DELETE", f"/crm/v1/visas/{ID}", None, "delete"),
    ("POST", f"/crm/v1/visas/{ID}/checklist", {"label": "Photo"}, "add_item"),
    ("PATCH", f"/crm/v1/visa-checklist/{ID}", {"done": True}, "set_item"),
    ("DELETE", f"/crm/v1/visa-checklist/{ID}", None, "delete_item"),
    ("GET", "/crm/v1/visa-templates", None, "list_templates"),
    ("POST", "/crm/v1/visa-templates", TEMPLATE, "create_template"),
    ("PATCH", f"/crm/v1/visa-templates/{ID}", {"active": False}, "update_template"),
    ("DELETE", f"/crm/v1/visa-templates/{ID}", None, "delete_template"),
    ("GET", "/crm/v1/settings/travel", None, "settings_view"),
    ("PUT", "/crm/v1/settings/travel", {"passport_valid_months": 3}, "update_settings"),
]
OWNER_ONLY = ("create_template", "update_template", "delete_template", "update_settings")


def test_every_travel_ops_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch,
                                                   calls: list) -> None:  # fmt: skip
    client, h = _client(pools, monkeypatch, "owner")
    for method, path, body, _ in REQUESTS:
        res = client.request(method, path, json=body, headers=h)
        assert res.status_code in (200, 201, 202), (method, path, res.text)
    assert calls == [r[3] for r in REQUESTS]
    for method, path, body in (
        ("PUT", f"/crm/v1/trips/{ID}/rooming", {"rooms": [{"room_no": 51, "room_type": "twin", "travellers": []}]}),
        ("POST", f"/crm/v1/trips/{ID}/pdfs", {"kind": "brochure"}),
        ("PUT", f"/crm/v1/lines/{ID}/voucher", {"service_time": "25:00"}),
        ("GET", "/crm/v1/departures/a%20b/roster", None),
        ("PUT", "/crm/v1/settings/travel", {"passport_valid_months": 30}),
    ):
        assert client.request(method, path, json=body, headers=h).status_code == 422, path


def test_the_agent_is_refused_the_owner_settings(pools: LocalPools, monkeypatch: pytest.MonkeyPatch,
                                                 calls: list) -> None:  # fmt: skip
    client, h = _client(pools, monkeypatch, "agent")
    for method, path, body, name in REQUESTS:
        res = client.request(method, path, json=body, headers=h)
        if name in OWNER_ONLY:
            assert res.status_code == 403, (path, res.text)
        else:
            assert res.status_code in (200, 201, 202), (path, res.text)
    assert not set(OWNER_ONLY) & set(calls)


# ── passport ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_passport_months_and_status() -> None:
    assert passport.add_months(dt.date(2031, 8, 31), 6) == dt.date(2032, 2, 29)
    assert passport.add_months(dt.date(2031, 1, 15), 0) == dt.date(2031, 1, 15)
    assert passport.add_months(dt.date(2031, 11, 30), 14) == dt.date(2033, 1, 30)
    assert passport.status(dt.date(2031, 9, 30), END, 6) == "ok"
    assert passport.status(dt.date(2031, 9, 29), END, 6) == "expires_too_soon"
    assert passport.status(None, END, 6) == "missing"
    assert passport.status(dt.date(2040, 1, 1), None, 6) == "no_trip_end"
    assert passport.needed_until(None, 6) is None
    assert passport.event_id(TRIP, dt.datetime(2031, 1, 1, tzinfo=dt.UTC)) == passport.event_id(
        TRIP, dt.datetime(2031, 1, 1, tzinfo=dt.UTC)
    )


def _party_row(traveler: uuid.UUID, expiry: str | None, role: str = "companion", blob: bytes | None = None) -> dict:
    value = blob if blob is not None else (enc("traveler", "passport_expiry_enc", expiry) if expiry else None)
    return {"trip_id": TRIP, "traveler_id": traveler, "role": role, "passport_expiry_enc": value, "end_date": END}


def test_passport_checks_and_settings() -> None:
    conn = (
        ScriptedConn()
        .on("SELECT passport_valid_months", None)
        .on(
            "FROM crm.trip_traveler pp",
            [
                _party_row(LEAD, "2031-12-01", "lead"),
                _party_row(MATE, "2031-05-01"),
                _party_row(uuid.uuid4(), None, blob=b"bad"),
            ],
        )  # fmt: skip
    )
    ctx = tenant_ctx(conn)
    view = run(passport.trip_view(ctx, FakeVault(), TRIP, END))
    assert [t["status"] for t in view["travellers"]] == ["ok", "expires_too_soon", "missing"]
    assert view["issues"] == 1 and view["valid_months"] == 6 and view["valid_until_needed"] == "2031-09-30"
    assert run(passport.issue_counts(ctx, FakeVault(), [TRIP])) == {TRIP: 1}
    assert run(passport.checks(ctx, FakeVault(), [])) == {}
    with pytest.raises(Invalid):
        run(passport.update_settings(ctx, True))
    conn.on("SELECT passport_valid_months", 3)
    assert run(passport.update_settings(ctx, 3))["passport_valid_months"] == 3
    assert conn.sql("INSERT INTO crm.tenant_settings (passport_valid_months)") == [(3,)]
    assert run(passport.issue_trips(ctx, FakeVault())) == {}  # no booked trip in the scripted stages


def test_booked_events_fire_only_for_an_issue() -> None:
    entered = dt.datetime(2031, 1, 2, tzinfo=dt.UTC)
    conn = (
        ScriptedConn()
        .on("WHERE stage = 'booked'", [{"trip_id": TRIP, "stage_entered_at": entered}])
        .on("FROM crm.trip_traveler pp", [_party_row(MATE, "2031-05-01")])
    )
    fired: list[Any] = []

    async def fire(_ctx: Any, trip: uuid.UUID, name: str, event: uuid.UUID) -> int:
        fired.append((trip, name, event))
        return 1

    counts = run(passport.booked_events(system_ctx(conn), FakeVault(), fire))
    assert counts == Counter(passport_events=1, passport_runs_started=1)
    assert fired == [(TRIP, passport.EVENT_NAME, passport.event_id(TRIP, entered))]
    assert run(passport.booked_events(system_ctx(ScriptedConn()), FakeVault(), fire)) == Counter()


# ── rooming ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _room(no: int, kind: str, *people: uuid.UUID, bed: str | None = None) -> dict[str, Any]:
    return {"room_no": no, "room_type": kind, "travellers": [{"traveler_id": str(p), "bed_pref": bed} for p in people]}


@pytest.mark.parametrize(
    ("rooms", "code"),
    [
        ("x", "invalid_rooming"),
        (["x"], "invalid_rooming"),
        ([{"room_no": 0, "room_type": "twin", "travellers": []}], "invalid_room_no"),
        ([_room(1, "twin", LEAD), _room(1, "single", MATE)], "room_no_twice"),
        ([_room(1, "suite", LEAD)], "invalid_room_type"),
        ([{"room_no": 1, "room_type": "twin", "travellers": []}], "room_empty"),
        ([_room(1, "single", LEAD, MATE)], "room_over_capacity"),
        ([{"room_no": 1, "room_type": "twin", "travellers": ["x"]}], "invalid_rooming"),
        ([{"room_no": 1, "room_type": "twin", "travellers": [{"traveler_id": "nope"}]}], "invalid_traveler_id"),
        ([_room(1, "twin", uuid.uuid4())], "traveler_not_on_trip"),
        ([_room(1, "twin", LEAD), _room(2, "single", LEAD)], "traveler_twice"),
        ([_room(1, "twin", LEAD, bed="hammock")], "invalid_bed_pref"),
        ([_room(1, "twin", LEAD), _room(2, "triple", MATE)], "rooms_not_in_proposal"),
    ],
)
def test_rooming_checks(rooms: Any, code: str) -> None:
    with pytest.raises(Invalid) as err:
        rooming.check(rooms, {LEAD, MATE}, Counter(twin=1))
    assert err.value.code == code


def test_rooming_view_replace_and_hold() -> None:
    assert rooming.config_counts(None) is None and rooming.config_counts('[{"type": "x"}]') is None
    assert rooming.config_counts('[{"type": "twin", "adults": 2}]') == Counter(twin=1)
    assert rooming.room_text(None) == "" and rooming.room_text({"room_no": 2, "room_type": "twin"}) == "2 twin"
    party = [
        {"traveler_id": LEAD, "role": "lead", "display_name_enc": enc("traveler", "display_name_enc", "Ilse Varo")},
        {"traveler_id": MATE, "role": "companion", "display_name_enc": enc("traveler", "display_name_enc", "Oto")},
    ]
    rows = [{"room_assignment_id": uuid.uuid4(), "traveler_id": LEAD, "room_no": 1, "room_type": "twin",
             "bed_pref": None}]  # fmt: skip
    conn = ScriptedConn().on("SELECT stage FROM crm.trip", "booked").on("FROM crm.trip_traveler tt", party)
    conn.on("FROM crm.room_assignment", rows).on("SELECT room_config", '[{"type": "twin", "adults": 2}]')
    view = run(rooming.view(tenant_ctx(conn), FakeVault(), TRIP))
    assert view["rooms"][0]["travellers"][0]["name"] == "Ilse Varo" and view["unassigned"][0]["name"] == "Oto"
    assert view["room_config"] == {"twin": 1}
    out = run(rooming.replace(tenant_ctx(conn), FakeVault(), TRIP, [_room(1, "twin", LEAD, MATE, bed="twin_beds")]))
    assert out["trip_id"] == str(TRIP)
    assert len(conn.sql("UPDATE crm.room_assignment")) == 1 and len(conn.sql("INSERT INTO crm.room_assignment")) == 1
    conn.on("DELETE FROM crm.room_assignment", Refusal("CRM10"))
    with pytest.raises(Conflict) as err:
        run(rooming.replace(tenant_ctx(conn), FakeVault(), TRIP, []))
    assert err.value.code == "legal_hold"
    conn.on("DELETE FROM crm.room_assignment", Refusal("XX000"))
    with pytest.raises(asyncpg.PostgresError):
        run(rooming.replace(tenant_ctx(conn), FakeVault(), TRIP, []))
    with pytest.raises(NotFound):
        run(rooming.view(tenant_ctx(ScriptedConn()), FakeVault(), TRIP))
    assert run(rooming.by_traveller(tenant_ctx(conn), TRIP))[LEAD]["room_no"] == 1


def test_rooming_save_without_a_bed_pref_keeps_the_one_on_file() -> None:
    beds = {LEAD: "extra_bed", MATE: None}
    rooms = [{"room_no": 1, "room_type": "twin", "travellers": [{"traveler_id": str(LEAD)}, {"traveler_id": "x"}]},
             {"room_no": 2, "room_type": "single", "travellers": [{"traveler_id": str(MATE), "bed_pref": "cot"}]},
             "odd", {"room_no": 3, "travellers": None}]  # fmt: skip
    kept = rooming.keep_beds(rooms, beds)
    assert kept[0]["travellers"] == [{"traveler_id": str(LEAD), "bed_pref": "extra_bed"}, {"traveler_id": "x"}]
    assert kept[1] == rooms[1] and kept[2:] == rooms[2:] and rooming.keep_beds("x", beds) == "x"
    party = [{"traveler_id": LEAD, "role": "lead", "display_name_enc": enc("traveler", "display_name_enc", "Ilse")}]
    rows = [{"room_assignment_id": uuid.uuid4(), "traveler_id": LEAD, "room_no": 1, "room_type": "twin",
             "bed_pref": "extra_bed"}]  # fmt: skip
    conn = ScriptedConn().on("SELECT stage FROM crm.trip", "booked").on("FROM crm.trip_traveler tt", party)
    conn.on("FROM crm.room_assignment", rows)
    # The lead's portal save names the traveller only: the agent's extra bed stays, nothing is written.
    named_only = [{"room_no": 1, "room_type": "twin", "travellers": [{"traveler_id": str(LEAD)}]}]
    run(rooming.replace(tenant_ctx(conn), FakeVault(), TRIP, named_only))
    assert conn.sql("UPDATE crm.room_assignment") == []
    # A null clears it.
    run(rooming.replace(tenant_ctx(conn), FakeVault(), TRIP, [_room(1, "twin", LEAD)]))
    assert conn.sql("UPDATE crm.room_assignment")[0][1:] == (1, "twin", None)


# ── vouchers ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _voucher_conn(stage: str = "booked", visible: bool = True, kind: str = "agency") -> ScriptedConn:
    vid = uuid.uuid4()
    details = {"booking_line_voucher_id": vid, "booking_line_id": LINE, "service_time": dt.time(7, 30),
               "pickup_enc": enc("booking_line_voucher", "pickup_enc", "Pier 4"), "dropoff_enc": None,
               "pnr_enc": enc("booking_line_voucher", "pnr_enc", "QX7"), "note_enc": None}  # fmt: skip
    line = {"booking_line_id": LINE, "supplier_kind": kind, "title": "Bay cruise", "confirmation_no": "C-9",
            "service_from": dt.date(2031, 3, 2), "service_to": None, "visible_to_travelers": visible,
            "bk_booking_id": "BKG-AAAA-1111" if kind == "aa" else None,
            "departure_id": "DEP-1" if kind == "aa" else None,
            "agency_supplier_id": uuid.uuid4(), "supplier_name": "Calm Bay Boats", "supplier_type": "own",
            "category": "activity", "contact_name_enc": enc("agency_supplier", "contact_name_enc", "Rua")}  # fmt: skip
    trip = {"trip_id": TRIP, "title": "Bay days", "stage": stage, "start_date": dt.date(2031, 3, 1),
            "end_date": END, "party_adults": 2, "party_child_ages": [4]}  # fmt: skip
    lead = {"traveler_id": LEAD, "display_name_enc": enc("traveler", "display_name_enc", "Ilse"),
            "given_name_enc": enc("traveler", "given_name_enc", "Ilse"),
            "family_name_enc": enc("traveler", "family_name_enc", "Varo")}  # fmt: skip
    return (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id", trip)
        .on("WHERE l.trip_id = $1 AND l.status = 'confirmed'", [line])
        .on("FROM crm.booking_line_voucher WHERE trip_id", [details])
        .on("FROM crm.booking_line_voucher WHERE booking_line_id", details)
        .on("SELECT display_name FROM crm.tenant", "Lotus Trails")
        .on("tt.role = 'lead'", lead)
        .on("WHERE l.booking_line_id = $1", {"booking_line_id": LINE, "trip_id": TRIP, "status": "confirmed"})
    )


def test_voucher_views_and_release() -> None:
    ctx = tenant_ctx(_voucher_conn())
    out = run(vouchers.trip_vouchers(ctx, FakeVault(), TRIP, travellers_only=True))
    (v,) = out["vouchers"]
    assert out["lead_traveller"] == "Ilse Varo" and out["pax"] == 3 and v["pickup"] == "Pier 4"
    assert v["service_time"] == "07:30" and v["supplier"]["contact_name"] == "Rua" and v["released"]
    hidden = run(vouchers.trip_vouchers(tenant_ctx(_voucher_conn("proposal")), FakeVault(), TRIP))
    assert hidden["vouchers"][0]["not_released_reason"] == "trip_not_booked"
    assert run(vouchers.trip_vouchers(tenant_ctx(_voucher_conn("proposal")), FakeVault(), TRIP,
                                      travellers_only=True))["vouchers"] == []  # fmt: skip
    aa = run(vouchers.trip_vouchers(tenant_ctx(_voucher_conn(kind="aa")), FakeVault(), TRIP))["vouchers"][0]
    assert aa["supplier"] == {"name": "Adventure Asia", "category": "activity", "contact_name": None,
                              "aa_booking_ref": "BKG-AAAA", "aa_departure_id": "DEP-1"}  # fmt: skip
    assert vouchers.release_reason("booked", False) == "line_hidden"
    with pytest.raises(NotFound):
        run(vouchers.trip_vouchers(tenant_ctx(ScriptedConn()), FakeVault(), TRIP))
    assert run(vouchers.get_details(ctx, FakeVault(), LINE))["pnr"] == "QX7"
    with pytest.raises(NotFound):
        run(vouchers.get_details(tenant_ctx(ScriptedConn()), FakeVault(), LINE))


def test_voucher_details_write(monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_trip(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(vouchers.cancellation, "ensure_not_cancelled", open_trip)
    conn = _voucher_conn()
    saved = run(vouchers.save_details(tenant_ctx(conn), FakeVault(), LINE, {"pickup": " Pier 4 ", "pnr": "",
                                                                             "service_time": "07:45:12"}))  # fmt: skip
    assert saved["pickup"] == "Pier 4" and saved["pnr"] is None and saved["service_time"] == "07:45"
    assert len(conn.sql("UPDATE crm.booking_line_voucher")) == 1
    fresh = ScriptedConn().on("WHERE l.booking_line_id = $1", {"booking_line_id": LINE, "trip_id": TRIP,
                                                                  "status": "planning"})  # fmt: skip
    run(vouchers.save_details(tenant_ctx(fresh), FakeVault(), LINE, {"service_time": dt.time(9, 0)}))
    assert len(fresh.sql("INSERT INTO crm.booking_line_voucher")) == 1
    for body, code in (({"service_time": "7h"}, "invalid_service_time"), ({"note": "\x00"}, "invalid_note")):
        with pytest.raises(Invalid) as err:
            run(vouchers.save_details(tenant_ctx(fresh), FakeVault(), LINE, body))
        assert err.value.code == code
    gone = ScriptedConn().on("WHERE l.booking_line_id = $1", {"booking_line_id": LINE, "trip_id": TRIP,
                                                                 "status": "cancelled"})  # fmt: skip
    with pytest.raises(Invalid):
        run(vouchers.save_details(tenant_ctx(gone), FakeVault(), LINE, {}))
    with pytest.raises(NotFound):
        run(vouchers.save_details(tenant_ctx(ScriptedConn()), FakeVault(), LINE, {}))


# ── visas ────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"country": "Vietnam"}, "invalid_country"),
        ({"visa_type": "work"}, "invalid_visa_type"),
        ({"processing_days": 400}, "invalid_processing_days"),
        ({"fee": "x", "fee_currency": "USD"}, "invalid_fee"),
        ({"fee": "1.234", "fee_currency": "USD"}, "invalid_fee"),
        ({"fee": "10"}, "fee_needs_currency"),
        ({"fee": "10", "fee_currency": "usd"}, "invalid_fee_currency"),
        ({"official_url": "http://x.test"}, "invalid_official_url"),
        ({"active": "yes"}, "invalid_active"),
        ({"documents": "Photo"}, "invalid_documents"),
        ({"documents": [""]}, "invalid_label"),
    ],
)
def test_visa_template_checks(over: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as err:
        visas.template_values({"country": "VN", "visa_type": "evisa", **over})
    assert err.value.code == code


def test_visa_helpers() -> None:
    values = visas.template_values({"country": "vn", "visa_type": "embassy", "fee": "25", "fee_currency": "USD",
                                    "documents": ["Photo"], "official_url": "https://x.test/visa"})  # fmt: skip
    assert values[:2] == ("VN", "embassy") and values[3] == "25" and values[5] == '["Photo"]'
    assert visas.mask(None) is None and visas.mask("AB") == "•••" and visas.mask("EV123456") == "•••456"
    assert visas.default_deadline(None, 5) is None
    assert visas.default_deadline(dt.date(2031, 3, 1), None) == dt.date(2031, 2, 15)
    assert visas.destination_countries(["vietnam", "laos", "vietnam", "not_sure"]) == ["VN", "LA"]
    assert visas._fee("") is None and visas._fee(Decimal("3.5")) == Decimal("3.5")


def _app_row(status: str = "to_do", **over: Any) -> dict[str, Any]:
    row = {"visa_application_id": APP, "trip_id": TRIP, "traveler_id": LEAD, "visa_template_id": None,
           "country": "VN", "visa_type": "evisa", "status": status, "deadline": dt.date(2031, 2, 1),
           "submitted_on": None, "decided_on": None, "visa_no_enc": None, "valid_from_enc": None,
           "valid_until_enc": None, "updated_at": None}  # fmt: skip
    row.update(over)
    return row


def test_visa_application_moves() -> None:
    sent = dt.date.today() - dt.timedelta(days=10)
    conn = ScriptedConn().on("WHERE a.visa_application_id = $1", _app_row("submitted", submitted_on=sent))
    ctx = tenant_ctx(conn)
    out = run(
        visas.update(
            ctx,
            FakeVault(),
            APP,
            {
                "status": "approved",
                "visa_no": "ev 1234 567",
                "valid_from": "2031-02-01",
                "valid_until": dt.date(2031, 5, 1),
            },
        )
    )
    args = conn.sql("UPDATE crm.visa_application")[0]
    assert args[1] == "approved" and args[4] is not None and bytes(args[5]).endswith(b"EV1234567")
    assert out["visa_application_id"] == str(APP)
    for changes, code in (
        ({"status": "lost"}, "invalid_status"),
        ({"status": "to_do"}, "invalid_transition"),
        ({"decided_on": (sent - dt.timedelta(days=1)).isoformat()}, "decided_before_submitted"),
        ({"visa_no": "EV1"}, "visa_details_need_approval"),
        ({"deadline": "soon"}, "invalid_deadline"),
    ):
        with pytest.raises((Invalid, Conflict)) as err:
            run(visas.update(ctx, FakeVault(), APP, changes))
        assert err.value.code == code
    approved = ScriptedConn().on("WHERE a.visa_application_id = $1", _app_row("approved", submitted_on=sent,
                                                                              decided_on=sent))  # fmt: skip
    with pytest.raises(Invalid):
        run(visas.update(tenant_ctx(approved), FakeVault(), APP, {"visa_no": "!!"}))
    approved.on("UPDATE crm.visa_application", asyncpg.CheckViolationError("x is not an allowed transition"))
    with pytest.raises(Conflict):
        run(visas.update(tenant_ctx(approved), FakeVault(), APP, {"status": "cancelled"}))
    approved.on("UPDATE crm.visa_application", asyncpg.CheckViolationError("visa_application_values_chk"))
    with pytest.raises(Invalid):
        run(visas.update(tenant_ctx(approved), FakeVault(), APP, {"status": "cancelled"}))
    with pytest.raises(NotFound):
        run(visas.update(tenant_ctx(ScriptedConn()), FakeVault(), APP, {}))
    with pytest.raises(NotFound):
        run(visas.get(tenant_ctx(ScriptedConn()), FakeVault(), APP))


def test_visa_create_refusals() -> None:
    booked = {"trip_id": TRIP, "stage": "booked", "start_date": dt.date(2031, 3, 1), "destinations": ["vietnam"]}
    party = [{"traveler_id": LEAD, "role": "lead", "display_name_enc": enc("traveler", "display_name_enc", "Ilse"),
              "nationality_enc": None, "passport_country_enc": None}]  # fmt: skip
    conn = ScriptedConn().on("SELECT trip_id, stage, start_date", booked).on("FROM crm.trip_traveler tt", party)
    ctx = tenant_ctx(conn)
    for body, err_type, code in (
        ({"traveler_id": "x"}, Invalid, "invalid_traveler_id"),
        ({"traveler_id": str(MATE), "country": "VN"}, Invalid, "traveler_not_on_trip"),
        ({"traveler_id": str(LEAD), "country": "VN", "visa_template_id": ID}, NotFound, "visa_template_not_found"),
        ({"traveler_id": str(LEAD), "country": "VN"}, Invalid, "invalid_visa_type"),
        ({"traveler_id": str(LEAD), "country": "VN", "visa_type": "evisa", "deadline": "x"}, Invalid,
         "invalid_deadline"),
    ):  # fmt: skip
        with pytest.raises(err_type) as err:
            run(visas.create(ctx, FakeVault(), TRIP, body))
        assert err.value.code == code
    conn.on("FROM crm.visa_template WHERE visa_template_id", {"visa_template_id": uuid.UUID(ID), "country": "LA"})
    with pytest.raises(Invalid):
        run(visas.create(ctx, FakeVault(), TRIP, {"traveler_id": str(LEAD), "country": "VN", "visa_template_id": ID}))
    conn.on("INSERT INTO crm.visa_application", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        run(visas.create(ctx, FakeVault(), TRIP, {"traveler_id": str(LEAD), "country": "VN", "visa_type": "evisa"}))
    conn.on("SELECT trip_id, stage, start_date", {**booked, "stage": "proposal"})
    with pytest.raises(Conflict):
        run(visas.create(ctx, FakeVault(), TRIP, {"traveler_id": str(LEAD), "country": "VN"}))
    with pytest.raises(NotFound):
        run(visas.suggest(tenant_ctx(ScriptedConn()), FakeVault(), TRIP))


def test_visa_templates_items_and_overdue() -> None:
    tpl = {"visa_template_id": uuid.UUID(ID), "country": "VN", "visa_type": "evisa", "processing_days": 5,
           "fee": Decimal("25.00"), "fee_currency": "USD", "documents": '["Photo"]', "official_url": None,
           "active": True, "updated_at": None}  # fmt: skip
    conn = ScriptedConn().on("FROM crm.visa_template WHERE deleted_at IS NULL", [tpl])
    conn.on("FROM crm.visa_template WHERE visa_template_id", tpl)
    ctx = tenant_ctx(conn)
    assert run(visas.list_templates(ctx))["templates"][0]["fee"] == "25.00"
    assert run(visas.update_template(ctx, uuid.UUID(ID), {"active": False, "bogus": 1}))["documents"] == ["Photo"]
    assert run(visas.delete_template(ctx, uuid.UUID(ID)))["deleted"]
    conn.on("UPDATE crm.visa_template", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        run(visas.update_template(ctx, uuid.UUID(ID), {"visa_type": "embassy"}))
    conn.on("INSERT INTO crm.visa_template", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        run(visas.create_template(ctx, {"country": "VN", "visa_type": "evisa"}))
    empty = tenant_ctx(ScriptedConn())
    for coro in (
        visas.update_template(empty, APP, {}),
        visas.delete_template(empty, APP),
        visas.delete(empty, APP),
        visas.add_item(empty, APP, "Photo"),
        visas.set_item(empty, APP, True),
        visas.delete_item(empty, APP),
    ):
        with pytest.raises(NotFound):
            run(coro)  # fmt: skip
    items = ScriptedConn().on("WHERE a.visa_application_id = $1", _app_row()).on("SELECT count(*)", 40)
    with pytest.raises(Conflict):
        run(visas.add_item(tenant_ctx(items), APP, "Photo"))
    item = {"visa_checklist_item_id": APP, "visa_application_id": APP, "trip_id": TRIP, "traveler_id": LEAD}
    ticks = ScriptedConn().on("FROM crm.visa_checklist_item i", item)
    assert run(visas.set_item(tenant_ctx(ticks), APP, True, by="traveler", traveler_id=LEAD))["done_by_kind"] == (
        "traveler"
    )
    with pytest.raises(NotFound):
        run(visas.set_item(tenant_ctx(ticks), APP, True, by="traveler", traveler_id=MATE))
    assert run(visas.delete_item(tenant_ctx(ticks), APP))["deleted"]
    held = ScriptedConn().on("WHERE a.visa_application_id = $1", _app_row()).on("DELETE FROM crm.visa_application",
                                                                                 Refusal("CRM10"))  # fmt: skip
    with pytest.raises(Conflict):
        run(visas.delete(tenant_ctx(held), APP))
    held.on("DELETE FROM crm.visa_application", Refusal("XX000"))
    with pytest.raises(asyncpg.PostgresError):
        run(visas.delete(tenant_ctx(held), APP))
    late = ScriptedConn().on("SELECT a.trip_id, count(*)", [{"trip_id": TRIP, "n": 2}])
    assert run(visas.overdue_tasks(system_ctx(late), dt.date(2031, 1, 1)))["visa_overdue_trips"] == 1


# ── departures ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_departure_helpers() -> None:
    assert departures.pax_of([{"party_adults": 2, "party_child_ages": [0, 1, 5]}]) == {
        "adults": 2, "children": 1, "infants": 2,
    }  # fmt: skip
    with pytest.raises(Invalid):
        departures.ref("a b")
    with pytest.raises(Invalid):
        departures.pick_fields(["passport_no"])
    assert departures.pick_fields(None) == departures.MANIFEST_DEFAULT
    assert departures.cell("=SUM(A1)") == "'=SUM(A1)" and departures.cell(None) == ""
    assert departures._audit_id("dep.1") is None and departures._audit_id("dep-1") == "dep-1"
    text, rows = departures.manifest_csv(
        [("abcd1234", {"given_name": "Ilse", "family_name": "Varo", "room": {"room_no": 1, "room_type": "twin"},
                       "arrival": {"flight_no": "ZZ1", "airport": "HAN"}, "dietary": ["vegan"]}, "ok")],
        departures.MANIFEST_FIELDS,
        "AADEP-1",
    )  # fmt: skip
    assert rows == 1 and "Ilse Varo,abcd1234,,1 twin,ok,vegan,,ZZ1 HAN," in text
    with pytest.raises(Invalid):
        run(departures.roster_list(tenant_ctx(), "soon", dt.date(2031, 1, 1)))
    with pytest.raises(NotFound):
        run(departures.roster(tenant_ctx(), FakeVault(), "DEP-1"))


def test_departure_list_and_feed() -> None:
    row = {"departure_id": "DEP-1", "title": "Bay loop", "service_from": dt.date(2031, 3, 1),
           "service_to": dt.date(2031, 3, 5), "trip_ids": [TRIP], "departure_status": None, "guaranteed_at": None,
           "cancelled_at": None, "min_to_operate": None}  # fmt: skip
    trip = {"trip_id": TRIP, "title": "Bay days", "stage": "booked", "start_date": None, "end_date": None,
            "party_adults": 2, "party_child_ages": [], "owner_member_id": None, "s3": 100}  # fmt: skip
    conn = ScriptedConn().on("GROUP BY l.departure_id", [row]).on("FROM crm.trip WHERE trip_id = ANY", [trip])
    listed = run(departures.roster_list(tenant_ctx(conn), "upcoming", dt.date(2031, 1, 1)))
    assert listed["departures"][0]["state"]["status"] == "scheduled"
    assert listed["departures"][0]["stage3_complete"] == 1
    when = dt.datetime(2031, 1, 1, tzinfo=dt.UTC)
    assert run(departures.apply_event(system_ctx(), "departure.time_change", "DEP-1", "e1", when)) == (
        "ignored", "departure_incomplete",
    )  # fmt: skip
    assert run(departures.apply_event(system_ctx(), "departure.guaranteed", "DEP-1", "e1", when)) == (
        "ignored", "no_feed_state",
    )  # fmt: skip
    feed = ScriptedConn().on("FROM crm.booking_feed_state", 1)
    assert run(departures.apply_event(system_ctx(feed), "departure.cancelled", "DEP-1", "e1", when)) == (
        "applied", "departure_cancelled",
    )  # fmt: skip
    assert feed.sql("INSERT INTO crm.aa_departure_state")[0][:3] == ("DEP-1", "cancelled", "e1")


# ── the PDF writer and its documents ─────────────────────────────────────────────────────────────────────────────
def test_pdf_writer() -> None:
    with pytest.raises(pdf.FontError):
        pdf.read_font(b"\x00" * 8)
    with pytest.raises(pdf.FontError):
        pdf.read_font(b"\x00\x01\x00\x00\x00\x00" + b"\x00" * 30)
    font = pdf.bundled_font()
    assert font.glyph("ệ") != 0 and font.glyph("\U0001f600") == 0
    assert pdf.wrap(font, "x" * 400, 10, 100)[0] and all(font.text_width(x, 10) <= 100 for x in pdf.wrap(
        font, "Xin chào " + "y" * 200, 10, 100))  # fmt: skip
    assert pdf.clean("a\tb\x07cé") == "a    bcé"
    assert pdf._pdf_text("Lotus") == "(Lotus)" and pdf._pdf_text("Hoà").startswith("<FEFF")
    assert pdf._num(-0.0001) == "0" and pdf._num(2.50) == "2.5"
    doc = pdf.Document(title="Thử", author="Lotus Trails", footer="Lotus Trails")
    doc.heading("Tiêu đề")
    doc.small("small")
    doc.space()
    doc.pairs([("A", "1"), ("B", None)])
    doc.table(["A", "B"], [["x", "y"]] * 80, right=(1,))
    doc.table(["", ""], [["Total", "1"]], show_header=False)
    data = doc.render(created=dt.datetime(2031, 1, 1, tzinfo=dt.UTC))
    assert data.startswith(b"%PDF-1.7") and data.rstrip().endswith(b"%%EOF") and len(doc.pages) >= 2
    assert b"/Author <FEFF" not in data and b"/Title <FEFF" in data and b"/FontFile2" in data


VIEW_INVOICE = {
    "currency": "USD", "status": "void", "void_reason": "duplicate", "number": "INV-7", "issue_date": "2031-01-02",
    "due_on": "2031-01-20", "tax_label": "VAT", "tax_rate": "8", "tax_inclusive": False, "tax_amount": "8.00",
    "subtotal_amount": "100.00", "total_amount": "108.00", "balance_amount": "8.00", "memo": "Thank you",
    "lines": [{"kind": "charge", "description": "Deposit", "quantity": 1, "unit_amount": "100", "amount": "100"},
              {"kind": "included", "description": "Bay cruise"}],
    "payments": [{"entry_kind": "payment", "amount": "110", "received_on": "2031-01-03"},
                 {"entry_kind": "refund", "amount": "10", "received_on": "2031-01-04"}],
}  # fmt: skip


@pytest.mark.parametrize("locale", ["en", "vi"])
def test_pdf_documents(locale: str) -> None:
    conf = renders.confirmation(
        {"agency_name": "Lotus Trails", "trip_title": "Bay days", "start_date": "2031-03-01", "end_date": "2031-03-05",
         "adults": 2, "children": 1, "party": [{"name": "Ilse", "role": "lead"}, {"name": "Oto", "role": "companion"}],
         "lines": [{"title": "Bay cruise", "service_from": "2031-03-02", "supplier": "Calm Bay Boats"}]},
        locale,
    )  # fmt: skip
    empty = renders.confirmation({"agency_name": None, "lines": []}, locale)
    inv = renders.invoice(VIEW_INVOICE, locale, agency="Lotus Trails", bill_to="Ilse",
                          pay_instructions=[{"display_name": "Bank", "instructions": "Account 1"}])  # fmt: skip
    draft = renders.invoice({**VIEW_INVOICE, "status": "draft", "tax_inclusive": True, "tax_amount": "0",
                             "payments": []}, locale, agency=None, bill_to=None, pay_instructions=[])  # fmt: skip
    prop = renders.proposal("Bay days, version 2", "Day 1\n\n**Bay cruise**", agency="Lotus Trails", locale=locale)
    vou = renders.voucher({"agency_name": "Lotus Trails", "lead_traveller": "Ilse", "pax": 3},
                          {"voucher_no": "ABCD", "title": "Bay cruise", "service_from": "2031-03-02",
                           "supplier": {"name": "Calm Bay Boats"}, "pickup": "Pier 4"}, locale)  # fmt: skip
    for data in (conf, empty, inv, draft, prop, vou):
        assert data.startswith(b"%PDF-")
    assert renders.span("2031-03-01", "2031-03-01", locale) == renders.day("2031-03-01", locale)
    assert renders.amount("x", "USD") == "x" and renders.amount(None, "USD") == ""
    assert renders.day(dt.date(2031, 3, 1), "en") == "1 Mar 2031"


# ── the PDF job ──────────────────────────────────────────────────────────────────────────────────────────────────
def _job(**payload: Any) -> Job:
    base = {"tenant_id": str(TENANT), "trip_id": str(TRIP), "kind": "confirmation"}
    return Job(id="1", kind="crm_pdf_render", payload={**base, **payload}, attempt=1, max_attempts=3)


def test_pdf_request_checks() -> None:
    trip = {"trip_id": TRIP, "stage": "booked", "title": "Bay days"}
    conn = ScriptedConn().on("FROM crm.trip WHERE trip_id", trip).on("INSERT INTO crm.job", "job-1")
    ctx = tenant_ctx(conn)
    assert run(pdf_job.request(ctx, TRIP, "confirmation", None))["queued"]
    assert conn.sql("INSERT INTO crm.job")[0][0] == "crm_pdf_render"
    conn.on("FROM crm.invoice", TRIP).on("FROM crm.proposal", MATE)
    assert run(pdf_job.request(ctx, TRIP, "invoice", APP))["document_kind"] == "invoice_pdf"
    for kind, subject, err_type in (("brochure", None, Invalid), ("invoice", None, Invalid),
                                    ("proposal", APP, NotFound), ("voucher", APP, NotFound)):  # fmt: skip
        with pytest.raises(err_type):
            run(pdf_job.request(ctx, TRIP, kind, subject))
    conn.on("SELECT trip_id, status FROM crm.booking_line", {"trip_id": TRIP, "status": "planning"})
    with pytest.raises(Conflict):
        run(pdf_job.request(ctx, TRIP, "voucher", LINE))
    conn.on("FROM crm.trip WHERE trip_id", {**trip, "stage": "proposal"})
    with pytest.raises(Conflict):
        run(pdf_job.request(ctx, TRIP, "confirmation", None))
    with pytest.raises(NotFound):
        run(pdf_job.request(tenant_ctx(ScriptedConn()), TRIP, "confirmation", None))


def test_pdf_build_invoice_and_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    trip = {"trip_id": TRIP, "title": "Bay days", "stage": "booked", "start_date": None, "end_date": None,
            "party_adults": 2, "party_child_ages": []}  # fmt: skip
    conn = ScriptedConn().on("FROM crm.trip WHERE trip_id", trip).on("SELECT display_name FROM crm.tenant", "Lotus")

    async def locale(_ctx: Any) -> str:
        return "en"

    async def full(_ctx: Any, _id: Any) -> dict[str, Any]:
        return {**VIEW_INVOICE, "trip_id": str(TRIP), "status": "open"}

    async def bill_to(*_: Any) -> str:
        return "Ilse"

    async def digest(*_: Any) -> str:
        return "a" * 64

    async def snapshot(*_: Any) -> Any:
        return object()

    class Rendered:
        subject = "Bay days, version 2"
        body = "Day 1\n\nBay cruise"

    monkeypatch.setattr(pdf_job.prep_packs, "agency_locale", locale)
    monkeypatch.setattr(pdf_job.invoices, "full", full)
    monkeypatch.setattr(pdf_job.invoices, "bill_to", bill_to)
    monkeypatch.setattr(pdf_job.text_proposal, "current_hash", digest)
    monkeypatch.setattr(pdf_job.text_proposal, "snapshot", snapshot)
    monkeypatch.setattr(pdf_job.text_render, "render", lambda *_a, **_k: Rendered())
    ctx = tenant_ctx(conn)
    data, name = run(pdf_job.build(ctx, FakeVault(), TRIP, "invoice", APP))
    assert data.startswith(b"%PDF") and name == "invoice-INV-7.pdf"
    conn.on("FROM crm.proposal p JOIN crm.trip", {"trip_id": TRIP, "version": 2})
    data, name = run(pdf_job.build(ctx, FakeVault(), TRIP, "proposal", APP))
    assert name == f"proposal-{str(TRIP)[:8]}-v2.pdf"
    conn.on("FROM crm.proposal p JOIN crm.trip", {"trip_id": MATE, "version": 2})
    for kind, subject in (("proposal", APP), ("voucher", APP), ("invoice", None)):
        with pytest.raises(NotFound):
            run(pdf_job.build(ctx, FakeVault(), TRIP, kind, subject))

    async def other_trip(_ctx: Any, _id: Any) -> dict[str, Any]:
        return {**VIEW_INVOICE, "trip_id": str(MATE)}

    monkeypatch.setattr(pdf_job.invoices, "full", other_trip)
    with pytest.raises(NotFound):
        run(pdf_job.build(ctx, FakeVault(), TRIP, "invoice", APP))
    with pytest.raises(NotFound):
        run(pdf_job.build(tenant_ctx(ScriptedConn()), FakeVault(), TRIP, "confirmation", None))


def test_pdf_handler_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PermanentError):
        run(pdf_job.build_handler(None, None)(object(), _job(trip_id="x")))  # type: ignore[arg-type]
    with pytest.raises(PermanentError):
        run(pdf_job.build_handler(None, None)(object(), _job(kind="brochure")))  # type: ignore[arg-type]
    assert run(pdf_job.build_handler(None, None)(object(), _job()))["reason_code"] == "not_configured"  # type: ignore[arg-type]

    @asynccontextmanager
    async def tx(*_: Any, **__: Any) -> Any:
        yield system_ctx()

    monkeypatch.setattr(pdf_job, "system_tx", tx)
    monkeypatch.setattr(pdf_job, "Vault", lambda *_: FakeVault())
    handler = pdf_job.build_handler(object(), object())  # type: ignore[arg-type]

    async def gone(*_: Any) -> Any:
        raise NotFound("trip_not_found")

    monkeypatch.setattr(pdf_job, "build", gone)
    assert run(handler(object(), _job(subject_id=ID)))["reason_code"] == "trip_not_found"  # type: ignore[arg-type]

    async def denied(*_: Any) -> Any:
        raise DecryptionDenied("integrity_check_failed")

    monkeypatch.setattr(pdf_job, "build", denied)
    with pytest.raises(PermanentError):
        run(handler(object(), _job()))  # type: ignore[arg-type]

    async def made(*_: Any) -> tuple[bytes, str]:
        return b"%PDF-1.7 x", "confirmation-x.pdf"

    async def upload(*_: Any, **kw: Any) -> dict[str, Any]:
        assert kw["uploaded_by_kind"] == "system" and kw["kind"] == "confirmation_pdf"
        return {"document_id": ID}

    monkeypatch.setattr(pdf_job, "build", made)
    monkeypatch.setattr(pdf_job.documents, "upload", upload)
    assert run(handler(object(), _job())) == {"document_id": ID, "kind": "confirmation", "byte_count": 10}  # type: ignore[arg-type]


# ── the portal wrappers ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def portal_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world: dict[str, Any] = {"role": "lead", "unlocked": True, "calls": []}
    ctx = system_ctx()

    @asynccontextmanager
    async def link_tx(*_: Any, **__: Any) -> Any:
        yield ctx, object()

    async def access(*_: Any) -> dict[str, Any]:
        return {"trip_id": TRIP, "traveler_id": LEAD, "access_id": APP, "role": world["role"]}

    async def signed(_ctx: Any, acc: Any, _session: Any) -> Any:
        return acc

    async def unlocked(*_: Any) -> bool:
        return world["unlocked"]

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    def stub(name: str, result: Any) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            world["calls"].append((name, kwargs))
            return result

        return fn

    room = {"rooms": [{"room_no": 1, "travellers": [{"traveler_id": str(LEAD)}]},
                      {"room_no": 2, "travellers": [{"traveler_id": str(MATE)}]}],
            "unassigned": [{"x": 1}]}  # fmt: skip
    monkeypatch.setattr(portal, "client_link_tx", link_tx)
    monkeypatch.setattr(portal, "_access", access)
    monkeypatch.setattr(portal, "_signed_in", signed)
    monkeypatch.setattr(portal, "_unlocked", unlocked)
    monkeypatch.setattr(portal, "check_origin", lambda *_: None)
    monkeypatch.setattr(portal, "Vault", lambda *_: FakeVault())
    monkeypatch.setattr(portal.stages, "touch", nothing)
    monkeypatch.setattr(portal.rooming, "view", stub("view", room))
    monkeypatch.setattr(portal.rooming, "replace", stub("replace", room))
    monkeypatch.setattr(
        portal.vouchers,
        "trip_vouchers",
        stub(
            "vouchers",
            {
                "vouchers": [{"title": "Bay cruise"}],
                "agency_name": "Lotus Trails",
                "trip_title": "Bay days",
                "start_date": None,
                "end_date": None,
                "lead_traveller": "Ilse Varo",
                "pax": 2,
            },
        ),
    )
    monkeypatch.setattr(portal.visas, "trip_applications", stub("visas", {"applications": []}))
    monkeypatch.setattr(portal.visas, "set_item", stub("set_item", {"done": True}))
    return world


async def _portal_calls(world: dict[str, Any]) -> list[dict[str, Any]]:
    out = [await portal.travel(object(), object(), "t" * 43, "s")]  # type: ignore[arg-type]
    world["role"] = "companion"
    out.append(await portal.travel(object(), object(), "t" * 43, "s"))  # type: ignore[arg-type]
    return out


def test_portal_travel_rooming_and_checklist(portal_world: dict[str, Any]) -> None:
    lead, companion = run(_portal_calls(portal_world))
    assert lead["rooming"]["can_edit"] and len(lead["rooming"]["rooms"]) == 2
    assert not companion["rooming"]["can_edit"] and companion["rooming"]["rooms"][0]["room_no"] == 1
    assert companion["rooming"]["unassigned"] == [] and lead["vouchers"] == [{"title": "Bay cruise"}]
    assert lead["voucher_header"] == {"agency_name": "Lotus Trails", "trip_title": "Bay days", "start_date": None,
                                      "end_date": None, "lead_traveller": "Ilse Varo", "pax": 2}  # fmt: skip
    assert ("vouchers", {"travellers_only": True}) in portal_world["calls"]
    with pytest.raises(Forbidden):
        run(portal.save_rooming(object(), object(), object(), "t", "s", {"rooms": []}, origin=None))  # type: ignore[arg-type]
    portal_world["role"] = "lead"
    assert run(portal.save_rooming(object(), object(), object(), "t", "s", {"rooms": []}, origin=None))["can_edit"]  # type: ignore[arg-type]
    portal_world["unlocked"] = False
    with pytest.raises(Forbidden):
        run(portal.save_rooming(object(), object(), object(), "t", "s", {"rooms": []}, origin=None))  # type: ignore[arg-type]
    out = run(portal.tick_visa_item(object(), object(), "t", "s", ID, {"done": True}, origin=None))  # type: ignore[arg-type]
    assert out == {"done": True} and portal_world["calls"][-1] == ("set_item", {"by": "traveler", "traveler_id": LEAD})
    with pytest.raises(NotFound):
        run(portal.tick_visa_item(object(), object(), "t", "s", "x", {"done": True}, origin=None))  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        run(portal.tick_visa_item(object(), object(), "t", "s", ID, {"done": "yes"}, origin=None))  # type: ignore[arg-type]


def test_roster_and_merged_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"departure_id": "DEP-1", "title": "Bay loop", "service_from": dt.date(2031, 3, 1), "service_to": None,
           "trip_ids": [TRIP], "departure_status": "guaranteed",
           "guaranteed_at": dt.datetime(2031, 1, 1, tzinfo=dt.UTC),
           "cancelled_at": None, "min_to_operate": 4}  # fmt: skip
    trip = {"trip_id": TRIP, "title": "Bay days", "stage": "booked", "start_date": dt.date(2031, 3, 1),
            "end_date": END, "party_adults": 2, "party_child_ages": [1], "owner_member_id": None, "s3": 50}  # fmt: skip
    party = [{"trip_id": TRIP, "traveler_id": LEAD, "role": "lead", "details_done": True,
              "display_name_enc": enc("traveler", "display_name_enc", "Ilse")},
             {"trip_id": TRIP, "traveler_id": MATE, "role": "companion", "details_done": False,
              "display_name_enc": enc("traveler", "display_name_enc", "Oto")}]  # fmt: skip
    conn = ScriptedConn().on("WHERE l.supplier_kind = 'aa' AND l.departure_id = $1", row)
    conn.on("FROM crm.trip WHERE trip_id = ANY", [trip]).on("AS details_done", party)

    async def checks(*_: Any) -> dict[uuid.UUID, list[dict[str, Any]]]:
        return {TRIP: [{"traveler_id": str(LEAD), "status": "ok"}, {"traveler_id": str(MATE),
                                                                     "status": "expires_too_soon"}]}  # fmt: skip

    async def rooms(*_: Any) -> dict[uuid.UUID, dict[str, Any]]:
        return {LEAD: {"room_no": 1, "room_type": "twin", "bed_pref": None}}

    class Built:
        text = json.dumps(
            {"travellers": [{"traveler_id": str(LEAD), "name": "Ilse", "room": {"room_no": 1, "room_type": "twin"}}]}
        )

    async def document(*_: Any) -> Built:
        return Built()

    monkeypatch.setattr(departures.passport, "checks", checks)
    monkeypatch.setattr(departures.rooming, "by_traveller", rooms)
    monkeypatch.setattr(departures.build, "document", document)
    ctx = tenant_ctx(conn, role="assistant")
    out = run(departures.roster(ctx, FakeVault(), "DEP-1"))
    assert out["state"]["status"] == "guaranteed" and out["pax"] == {"adults": 2, "children": 0, "infants": 1}
    assert out["trips"][0]["passport_issues"] == 1 and out["trips"][0]["rooms_placed"] == 1
    assert [t["passport"] for t in out["travellers"]] == ["ok", "expires_too_soon"]
    assert ctx.audit_events[-1].reason == "departure_roster"
    made = run(departures.manifest(ctx, FakeVault(), "DEP-1", ["name", "room", "passport_check"]))
    assert (
        made["row_count"] == 1 and ",Ilse,1 twin,ok" in made["csv"] and made["filename"].startswith("departure-DEP-1")
    )
    assert made["reason"] == "duty_of_care"
    assert [(e.action, e.object_id, e.reason) for e in ctx.audit_events[-2:]] == [
        ("reveal", str(LEAD), "duty_of_care"),
        ("report_export", "DEP-1", "departure_manifest"),
    ]


def test_merged_manifest_is_a_bulk_reveal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reason above five travellers or with a health column; the hourly reveal limit before anything opens."""
    assert departures.manifest_reason(None, ("name",), 5) == "duty_of_care"
    assert departures.manifest_reason("ops_handoff", ("name", "dietary"), 1) == "ops_handoff"
    for reason, picked, count in ((None, ("name",), 6), (None, ("mobility",), 1), ("because", ("name",), 1)):
        with pytest.raises(Invalid) as err:
            departures.manifest_reason(reason, picked, count)
        assert err.value.code == "reason_required"
    row = {"departure_id": "DEP-1", "trip_ids": [TRIP]}
    trip = {"trip_id": TRIP}
    conn = ScriptedConn().on("WHERE l.supplier_kind = 'aa' AND l.departure_id = $1", row)
    conn.on("FROM crm.trip WHERE trip_id = ANY", [trip]).on("SELECT count(*) FROM crm.trip_traveler", 12)

    async def document(*_: Any) -> Any:
        raise AssertionError("nothing is opened before the checks")

    monkeypatch.setattr(departures.build, "document", document)
    ctx = tenant_ctx(conn)
    with pytest.raises(Invalid) as err:
        run(departures.manifest(ctx, FakeVault(), "DEP-1", ["name"]))
    assert err.value.code == "reason_required"
    meter = reveal_meter.RevealMeter()
    monkeypatch.setattr(reveal_meter, "METER", meter)
    meter.record(TENANT, ctx.member_id, departures.duty.HOURLY_LIMIT - 11)
    with pytest.raises(RateLimited) as limited:
        run(departures.manifest(ctx, FakeVault(), "DEP-1", ["name"], "ops_handoff"))
    assert limited.value.code == "reveal_rate_limited"
    denied = ctx.audit_events[-1]
    assert (denied.action, denied.outcome, denied.reason, denied.object_ids) == ("reveal", "denied", "rate_limited",
                                                                                 (str(TRIP),))  # fmt: skip


def test_visa_create_suggest_and_list() -> None:
    booked = {
        "trip_id": TRIP,
        "stage": "booked",
        "start_date": dt.date(2031, 3, 1),
        "destinations": ["vietnam", "laos"],
    }
    party = [{"traveler_id": LEAD, "role": "lead", "display_name_enc": enc("traveler", "display_name_enc", "Ilse"),
              "nationality_enc": enc("traveler", "nationality_enc", "VN"), "passport_country_enc": None},
             {"traveler_id": MATE, "role": "companion", "display_name_enc": enc("traveler", "display_name_enc", "Oto"),
              "nationality_enc": None, "passport_country_enc": b"bad"}]  # fmt: skip
    tpl = {"visa_template_id": uuid.UUID(ID), "country": "LA", "visa_type": "visa_on_arrival", "processing_days": 0,
           "fee": None, "fee_currency": None, "documents": ["Photo", "Form"], "official_url": None, "active": True,
           "updated_at": None}  # fmt: skip
    item = {"visa_checklist_item_id": uuid.uuid4(), "visa_application_id": APP, "label": "Photo", "sort": 0,
            "done": True, "done_at": None, "done_by_kind": "traveler"}  # fmt: skip
    conn = (
        ScriptedConn()
        .on("SELECT trip_id, stage, start_date", booked)
        .on("FROM crm.trip_traveler tt", party)
        .on("AND active AND deleted_at IS NULL", tpl)
        .on("FROM crm.visa_template WHERE visa_template_id", tpl)
        .on("WHERE a.visa_application_id = $1", _app_row(country="LA", visa_template_id=uuid.UUID(ID)))
        .on("FROM crm.visa_checklist_item", [item])
    )
    ctx = tenant_ctx(conn)
    sug = run(visas.suggest(ctx, FakeVault(), TRIP))
    assert {(s["traveler_name"], s["country"]) for s in sug["suggestions"]} == {
        ("Ilse", "LA"), ("Oto", "VN"), ("Oto", "LA"),
    }  # fmt: skip
    made = run(visas.create(ctx, FakeVault(), TRIP, {"traveler_id": str(LEAD), "country": "la",
                                                     "visa_template_id": ID}))  # fmt: skip
    assert made["country"] == "LA" and [a[1] for a in conn.sql("INSERT INTO crm.visa_checklist_item")] == [
        "Photo", "Form",
    ]  # fmt: skip
    assert conn.sql("INSERT INTO crm.visa_application")[0][6] == dt.date(2031, 2, 15)  # start - 0 - 14 days
    conn.on("WHERE a.trip_id = $1 AND a.deleted_at IS NULL", [_app_row()])
    listed = run(visas.trip_applications(ctx, FakeVault(), TRIP))
    assert listed["applications"][0]["traveler_name"] == "Ilse" and listed["can_add"]
    mine = run(visas.trip_applications(ctx, FakeVault(), TRIP, traveler_id=LEAD))
    assert mine["applications"][0]["traveler_name"] is None


def test_pdf_build_confirmation_and_voucher(monkeypatch: pytest.MonkeyPatch) -> None:
    async def locale(_ctx: Any) -> str:
        return "vi"

    monkeypatch.setattr(pdf_job.prep_packs, "agency_locale", locale)
    conn = _voucher_conn()
    conn.on("FROM crm.trip_traveler pt", [{"traveler_id": LEAD, "role": "lead",
                                           "display_name_enc": enc("traveler", "display_name_enc",
                                                                   "Ilse")}])  # fmt: skip
    data, name = run(pdf_job.build(tenant_ctx(conn), FakeVault(), TRIP, "confirmation", None))
    assert data.startswith(b"%PDF") and name == f"confirmation-{str(TRIP)[:8]}.pdf"
    data, name = run(pdf_job.build(tenant_ctx(conn), FakeVault(), TRIP, "voucher", LINE))
    assert name == f"voucher-{str(LINE)[:8].upper()}.pdf"


def test_calendar_aa_departure_layer() -> None:
    from app.calendar import events as calendar_events
    from app.calendar import ics

    row = {"departure_id": "DEP-1", "title": "Bay loop", "day": dt.date(2031, 3, 1), "trips": 2,
           "departure_status": "guaranteed"}  # fmt: skip
    conn = ScriptedConn().on("GROUP BY l.departure_id, s.departure_status", [row])
    out = run(calendar_events.events(tenant_ctx(conn), start=dt.date(2031, 3, 1), end=dt.date(2031, 3, 31),
                                     member_id=None, tz=dt.UTC))  # fmt: skip
    assert [(e["kind"], e["departure_id"], e["trips"]) for e in out] == [("aa_departure", "DEP-1", 2)]
    assert ics.LABELS["aa_departure"] == "AA departure"


def test_travelling_counts_passport_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.duty import service as duty

    trip = {"trip_id": TRIP, "title": "Bay days", "stage": "travelling", "start_date": dt.date(2031, 3, 1),
            "end_date": END, "owner_member_id": None, "destinations": ["vietnam"]}  # fmt: skip

    async def counts(*_: Any) -> dict[uuid.UUID, int]:
        return {TRIP: 2}

    async def today(_ctx: Any) -> tuple[dt.date, str]:
        return dt.date(2031, 3, 2), "Asia/Ho_Chi_Minh"

    async def flags(*_: Any) -> list[Any]:
        return []

    monkeypatch.setattr(duty.passport, "issue_counts", counts)
    monkeypatch.setattr(duty, "today", today)
    monkeypatch.setattr(duty, "_open_flags", flags)
    monkeypatch.setattr(duty, "where_on", lambda *_: ("vietnam", None, 2, []))
    conn = ScriptedConn().on("FROM crm.trip t", [trip])
    out = run(duty.travelling(tenant_ctx(conn), vault=FakeVault()))
    (row,) = [r for region in out["regions"] for r in region["trips"]]
    assert row["passport_issues"] == 2
