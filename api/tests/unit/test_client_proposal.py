"""AA-950 (card CRM-21b) without a database: the client's choice and what it shows (D1 deltas, R26 groups, Fix B9),
the hold box and the re-check limit (Fix A3, card R), the hold set against a fake Booking (card E sold out, Booking
down, Booking's own re-hold limit), the event detail cap, and the routes (the proposal cookie of Fix B11, the agency
routes and their roles). The end-to-end cases run in ``tests/integration/test_client_proposal_api.py``."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.proposals import client_page, events, holds, service
from app.proposals import public as proposal_page
from app.routers import client_pages
from tests.aa_fakes import FakeBooking, booking_client
from tests.tokens import LocalPools
from tests.unit.test_thin_lifecycle_routes import ID, TOKEN, _agency_client, _pool, _stub, pools  # noqa: F401

NOW = dt.datetime(2026, 10, 26, 10, 0, tzinfo=dt.UTC)
P, DAY1, DAY2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
LODGE, TOUR = uuid.uuid4(), uuid.uuid4()
L1, L2, T1, T2 = (uuid.uuid4() for _ in range(4))


def _o(oid: uuid.UUID, block: uuid.UUID, title: str, sell: str, *, source: str = "agency", avail: str = "agency",
       later: bool = False, poa: bool = False, source_option: uuid.UUID | None = None) -> dict[str, Any]:  # fmt: skip
    return {"option_id": oid, "option_block_id": block, "title": title, "sell_amount": Decimal(sell),
            "net_amount": Decimal("1"), "source": source, "availability": avail, "choose_later_allowed": later,
            "pay_on_arrival": poa, "source_option_id": source_option, "aa_component_id": "tp:1",
            "aa_departure_id": None, "aa_nights": None}  # fmt: skip


def _s(*, tour_avail: str = "instant") -> dict[str, Any]:
    return {
        "days": [
            {"proposal_day_id": DAY1, "day_no": 1, "title": "Arrive", "place": None, "tz": "Asia/Kathmandu",
             "notes_enc": None},
            {"proposal_day_id": DAY2, "day_no": 2, "title": None, "place": None, "tz": "Asia/Kathmandu",
             "notes_enc": None},
        ],
        "blocks": [
            {"option_block_id": LODGE, "proposal_day_id": DAY1, "title": "Lodging", "type": "lodging",
             "client_may_swap": True, "default_option_id": L1, "source_block_id": None},
            {"option_block_id": TOUR, "proposal_day_id": DAY2, "title": "Day tour", "type": "activity",
             "client_may_swap": True, "default_option_id": T1, "source_block_id": None},
        ],
        "options": [
            _o(L1, LODGE, "Lodge", "250", later=True),
            _o(L2, LODGE, "Riverside", "290"),
            _o(T1, TOUR, "Valley tour", "500", source="aa", avail=tour_avail),
            _o(T2, TOUR, "Valley tour upgrade", "650", source="aa", avail="on_request"),
        ],
        "rules": [],
    }  # fmt: skip


def _pick(block: uuid.UUID, option: uuid.UUID, later: bool = False) -> dict[str, Any]:
    return {"option_block_id": block, "option_id": option, "chosen_by": "client", "choose_later": later}


def _row(**over: Any) -> dict[str, Any]:
    row = {"proposal_id": P, "version": 2, "currency": "USD", "deposit_pct": Decimal("30"), "balance_due_on": None,
           "price_visibility": "total_with_deltas", "room_config": '[{"type": "double", "adults": 2, "children": 0}]',
           "starts_on": dt.date(2026, 12, 1), "party_adults": 2, "party_child_ages": [], "hold_id": None,
           "hold_quote_id": None, "hold_expires_at": None, "hold_selection_sha256": None, "hold_count": 0,
           "recheck_day": None, "recheck_count_day": 0}  # fmt: skip
    row.update(over)
    return row


# ── the choice ───────────────────────────────────────────────────────────────────────────────────


def test_a_sold_out_pick_falls_back_to_the_default() -> None:
    s = _s()
    s["options"][3]["availability"] = "sold_out"
    state = client_page.effective(s, [_pick(LODGE, L2), _pick(TOUR, T2)])
    assert state["choice"] == {str(LODGE): str(L2), str(TOUR): str(T1)} and state["dropped"] == [str(TOUR)]
    assert client_page.totals(s, _row(), state["picks"])["total"] == "790.00"


def test_selection_hash_follows_the_choice_and_choose_later() -> None:
    s = _s()
    a = client_page.effective(s, [])
    b = client_page.effective(s, [_pick(LODGE, L1, later=True)])
    c = client_page.effective(s, [_pick(LODGE, L2)])
    hashes = {client_page.selection_hash(x["choice"], x["later"]) for x in (a, b, c)}
    assert len(hashes) == 3 and all(len(h) == 64 for h in hashes)


def test_confirm_classes_and_choose_later_only_for_agency_items() -> None:
    s = _s()
    opts = {str(o["option_id"]): o for o in s["options"]}
    assert [client_page.confirm_class(opts[str(x)]) for x in (L1, T1, T2)] == ["agent", "on_payment", "partner"]
    assert client_page.confirm_class({**opts[str(L1)], "pay_on_arrival": True}) == "arrival"
    assert client_page.choose_later_ok(s["blocks"][0], opts) and not client_page.choose_later_ok(s["blocks"][1], opts)


@pytest.mark.asyncio
async def test_the_view_shows_deltas_or_only_the_total() -> None:
    s = _s()
    shown = await client_page.view(object(), object(), _row(), s, [_pick(LODGE, L2)], NOW)  # type: ignore[arg-type]
    lodge = shown["days"][0]["blocks"][0]["options"]
    assert [o["delta"] for o in lodge] == ["0.00", "40.00"] and lodge[1]["chosen"]
    assert shown["totals"]["total"] == "790.00" and shown["totals"]["deposit"] == "237.00"
    assert shown["days"][0]["utc_offset"] == "+05:45" and shown["hold"]["status"] == "missing"
    assert {g["confirm_class"] for g in shown["groups"]} == {"agent", "on_payment"}
    assert "net_amount" not in str(shown) and "sell_amount" not in str(shown)
    only = await client_page.view(object(), object(), _row(price_visibility="total_only"), s, [], NOW)  # type: ignore[arg-type]
    assert all(o["delta"] is None for d in only["days"] for b in d["blocks"] for o in b["options"])


def test_the_hold_box() -> None:
    aa = [str(T1)]
    digest = client_page.aa_hash(aa)
    held = {"hold_id": "h1", "hold_selection_sha256": digest, "hold_count": 1}
    assert client_page.hold_state(_row(), [], NOW)["status"] == "none"
    assert client_page.hold_state(_row(), aa, NOW)["status"] == "missing"
    active = client_page.hold_state(_row(**held, hold_expires_at=NOW + dt.timedelta(hours=1)), aa, NOW)
    assert active["status"] == "active" and active["rechecks_left"] == 3
    expired = _row(**held, hold_expires_at=NOW - dt.timedelta(seconds=1), recheck_day=NOW.date(), recheck_count_day=2)
    assert client_page.hold_state(expired, aa, NOW)["status"] == "expired"
    assert client_page.hold_state(expired, aa, NOW)["rechecks_left"] == 1
    yesterday = {**expired, "recheck_day": NOW.date() - dt.timedelta(days=1)}
    assert client_page.hold_state(yesterday, aa, NOW)["rechecks_left"] == 3
    assert client_page.hold_state(_row(**held, hold_expires_at=NOW + dt.timedelta(hours=1)), [str(T2)], NOW)[
        "status"] == "stale"  # fmt: skip


def test_r19_the_choice_diff_and_the_notice_of_a_new_version() -> None:
    s = _s()
    diff = client_page.choice_diff(s, [_pick(LODGE, L2), _pick(TOUR, T1)])
    assert diff == [{"day_no": 1, "block": "Lodging", "default": "Lodge", "chosen": "Riverside", "sell_delta": "40.00",
                     "choose_later": False, "fell_back": False, "chosen_by": "client"}]  # fmt: skip
    cur = _s()
    cur["options"] = [
        _o(uuid.uuid4(), LODGE, "Lodge", "260", source_option=L1),
        _o(uuid.uuid4(), LODGE, "Jungle lodge", "300"),
        _o(uuid.uuid4(), TOUR, "Valley tour", "500", source="aa", avail="instant", source_option=T1),
    ]
    notice = client_page.version_notice(s, cur, Decimal("750"), Decimal("760"), 1)
    kinds = sorted((c["kind"], c.get("title")) for c in notice["changes"])
    assert kinds == [("added", "Jungle lodge"), ("changed", "Lodge"), ("removed", "Riverside"),
                     ("removed", "Valley tour upgrade")]  # fmt: skip
    assert notice["total_from"] == "750.00" and notice["total_to"] == "760.00" and notice["picks_carried"] == 1


# ── the hold set ─────────────────────────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


class _Ctx:
    def __init__(self) -> None:
        self.conn = _Conn()


@pytest.mark.asyncio
async def test_plan_reuses_reserves_and_refuses_the_fourth_recheck() -> None:
    s = _s()
    ctx = _Ctx()
    none = await holds.plan(ctx, _row(), {**s, "options": s["options"][:2], "blocks": s["blocks"][:1]}, [], NOW)
    assert none.action == "none"
    first = await holds.plan(ctx, _row(), s, [], NOW)
    assert first.action == "place" and not first.rehold and ctx.conn.executed == []  # the first hold is free
    assert first.idempotency_key == f"crm-proposal:{P}:v2:h1" and first.body["lines"][0]["line_ref"] == str(T1)
    assert first.body["lines"][0]["service_date"] == "2026-12-02" and first.body["lines"][0]["pax"]["adult"] == 2
    digest = client_page.aa_hash([str(T1)])
    held = {"hold_id": "h1", "hold_quote_id": "q1", "hold_selection_sha256": digest, "hold_count": 1}
    reuse = await holds.plan(ctx, _row(**held, hold_expires_at=NOW + dt.timedelta(hours=1)), s, [], NOW)
    assert reuse.action == "reuse"
    expired = _row(**held, hold_expires_at=NOW - dt.timedelta(minutes=1), recheck_day=NOW.date(), recheck_count_day=2)
    again = await holds.plan(ctx, expired, s, [], NOW)
    assert again.action == "place" and again.rehold and again.rechecks_left == 0 and again.release_hold_id is None
    assert ctx.conn.executed[-1][1] == (P, NOW.date(), 3)  # reserved before Booking is asked
    refused = await holds.plan(ctx, {**expired, "recheck_count_day": 3}, s, [], NOW)
    assert refused.action == "refused" and len(ctx.conn.executed) == 1
    # A changed AA pick while the old hold is still active: it is released first.
    changed = await holds.plan(ctx, _row(**held, hold_expires_at=NOW + dt.timedelta(hours=1)), s,
                               [_pick(TOUR, T2)], NOW)  # fmt: skip
    assert changed.action == "place" and changed.release_hold_id == "h1"


async def _placed(fake: FakeBooking, **kw: Any) -> holds.Placed:
    p = holds.Plan(action="place", proposal_id=P, version=2, aa_ids=[str(T1)],
                   body={"proposal_ref": f"crm-proposal:{P}", "proposal_version": 2, "currency": "USD",
                         "lines": [{"line_ref": str(T1), "component_id": "tp:1", "service_date": "2026-12-02",
                                    "pax": {"adult": 2, "child": 0, "infant": 0}}]},
                   idempotency_key=f"crm-proposal:{P}:v2:h1", **kw)  # fmt: skip
    return await holds.place(booking_client(fake), uuid.uuid4(), p)


@pytest.mark.asyncio
async def test_place_against_booking() -> None:
    fake = FakeBooking()
    held = await _placed(fake)
    assert held.outcome == "held" and held.hold is not None and held.hold.status == "active"
    assert fake.calls["POST quotes"] == 1 and fake.calls["POST holds"] == 1
    sold = FakeBooking(line_availability={str(T1): "sold_out"})
    out = await _placed(sold)
    assert out.outcome == "sold_out" and out.sold_out == [str(T1)] and sold.calls["POST holds"] == 0
    assert (await _placed(FakeBooking(hold_refusal="rehold_limit"))).code == "recheck_limit"
    assert (await _placed(FakeBooking(hold_refusal="sold_out"))).code == "hold_sold_out"
    assert (await _placed(FakeBooking(down=True))).outcome == "unavailable"
    release = FakeBooking()
    release.holds["h9"] = {"hold_id": "h9", "status": "active"}
    await _placed(release, release_hold_id="h9")
    assert release.calls["DELETE holds"] == 1 and release.holds["h9"]["status"] == "released"


@pytest.mark.asyncio
async def test_event_detail_is_capped_and_names_are_known() -> None:
    long = events._detail({"choices": [{"block": "x" * 100}] * 60, "version": 2, "reason": "rooms"})
    assert len(long.encode()) <= events.MAX_DETAIL and '"truncated":true' in long and '"version":2' in long
    with pytest.raises(ValueError):
        await events.record(_Ctx(), uuid.uuid4(), "proposal.unknown")  # type: ignore[arg-type]


# ── routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cookie_client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list]:  # noqa: F811
    settings = pools.settings(rate_limit_intake_per_minute=50)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    out: list[tuple[str, Any]] = []

    def stub(name: str, result: Any) -> Any:
        async def fn(*args: Any, **kw: Any) -> Any:
            out.append((name, args))
            return result

        return fn

    monkeypatch.setattr(proposal_page, "read", stub("read", {"status": "sent"}))
    monkeypatch.setattr(proposal_page, "pick", stub("pick", {"status": "sent"}))
    monkeypatch.setattr(proposal_page, "accept", stub("accept", {"status": "accepted"}))
    monkeypatch.setattr(proposal_page, "change_request", stub("change_request", {"status": "sent"}))
    monkeypatch.setattr(proposal_page, "hold", stub("hold", (429, {"detail": "recheck_limit"})))
    return TestClient(app, base_url="https://go.test"), out


def test_fix_b11_the_proposal_cookie(cookie_client: tuple[TestClient, list]) -> None:
    client, out = cookie_client
    with client:
        res = client.get("/crm/public/v1/proposal")
        assert res.status_code == 404 and res.json()["detail"] == "link_not_found" and out == []
        client.cookies.set(client_pages.PROPOSAL_COOKIE, "short")
        assert client.get("/crm/public/v1/proposal").status_code == 404
        client.cookies.set(client_pages.PROPOSAL_COOKIE, TOKEN)
        res = client.get("/crm/public/v1/proposal")
        assert res.status_code == 200 and res.headers["referrer-policy"] == "no-referrer"
        assert res.headers["cache-control"] == "no-store"
        assert client.post("/crm/public/v1/proposal/pick", json={"block_id": ID}).status_code == 200
        assert client.post("/crm/public/v1/proposal/accept", json={"full_name": "G"}).status_code == 200
        assert client.post("/crm/public/v1/proposal/change-request", json={"reason": "rooms"}).status_code == 200
        res = client.post("/crm/public/v1/proposal/hold")
        assert res.status_code == 429 and res.json() == {"detail": "recheck_limit"}
    assert [n for n, _ in out] == ["read", "pick", "accept", "change_request", "hold"]
    assert all(TOKEN in args for _, args in out)  # the cookie's token reaches the page, never another value


def test_the_agency_routes_of_the_client_side(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    out: list[tuple[str, Any]] = []
    for name in ("reissue_link", "client_activity"):
        monkeypatch.setattr(service, name, _stub(out, f"service.{name}"))
    monkeypatch.setattr(events, "bell", _stub(out, "events.bell", {"items": [], "unseen": 0}))
    monkeypatch.setattr(events, "mark_seen", _stub(out, "events.mark_seen", {"seen": 0}))
    client, h = _agency_client(pools, monkeypatch, "agent")
    with client:
        assert client.post(f"/crm/v1/proposals/{ID}/reissue-link", headers=h).status_code == 200
        assert client.get(f"/crm/v1/proposals/{ID}/client-activity", headers=h).status_code == 200
        assert client.get("/crm/v1/bell", headers=h).json() == {"items": [], "unseen": 0}
        assert client.post("/crm/v1/bell/seen", headers=h, json={"trip_ids": [ID]}).status_code == 200
        assert client.post("/crm/v1/bell/seen", headers=h, json={"x": 1}).status_code == 422
    assert [n for n, _ in out] == ["service.reissue_link", "service.client_activity", "events.bell",
                                   "events.mark_seen"]  # fmt: skip
    assistant, h = _agency_client(pools, monkeypatch, "assistant")
    with assistant:
        assert assistant.post(f"/crm/v1/proposals/{ID}/reissue-link", headers=h).status_code == 403
        assert assistant.get(f"/crm/v1/proposals/{ID}/client-activity", headers=h).status_code == 200
