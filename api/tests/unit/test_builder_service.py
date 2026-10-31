"""AA-949: :mod:`app.proposals.builder` against a scripted connection — every branch the routes reach: the lock and
its version conflict, the skeleton and the working copy, settings, days, blocks, options, AA items, rules, publish
(blocking checks, the client-pick conflict, carried picks) and picks. The database side (guards, RLS, erase) and the
end-to-end flows run on PostgreSQL in tests/integration/test_builder_api.py and test_crm019_schema.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.proposals import builder
from app.proposals.aa_options import AaQuote
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

pytestmark = pytest.mark.asyncio

P, TRIP, BASE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
D0, D1, D2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
B1, B2 = uuid.uuid4(), uuid.uuid4()
O1, O2, O3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
R1 = uuid.uuid4()
START = dt.date(2026, 12, 1)
NOW = dt.datetime.now(dt.UTC)


def _proposal(**over: Any) -> dict[str, Any]:
    row = {
        "proposal_id": P, "trip_id": TRIP, "version": 2, "status": "draft", "kind": "builder",
        "body_enc": b"enc:proposal.body_enc:Hello", "currency": "USD", "total_amount": Decimal(0),
        "deposit_amount": Decimal(0), "balance_due_on": dt.date(2026, 11, 1), "terms_doc_id": None,
        "price_hash": None, "price_visibility": "total_with_deltas",
        "room_config": '[{"type": "double", "adults": 2, "children": 0}]', "deposit_pct": Decimal("30"),
        "starts_on": START, "edit_seq": 5, "selection_seq": 0, "based_on_proposal_id": None,
        "based_on_selection_seq": None, "published_at": None, "sent_at": None, "created_at": NOW, "updated_at": NOW,
        "trip_stage": "proposal", "party_adults": 2, "party_child_ages": [], "party_type": "couple",
        "destinations": ["nepal"],
    }  # fmt: skip
    row.update(over)
    return row


def _days() -> list[dict[str, Any]]:
    return [
        {"proposal_day_id": D0, "day_no": 0, "title": None, "place": None, "tz": "Asia/Kathmandu", "notes_enc": None,
         "source_day_id": None},
        {"proposal_day_id": D1, "day_no": 1, "title": "Arrive", "place": "Kathmandu", "tz": "Asia/Kathmandu",
         "notes_enc": b"enc:proposal_day.notes_enc:Welcome", "source_day_id": None},
        {"proposal_day_id": D2, "day_no": 2, "title": None, "place": None, "tz": "Asia/Kathmandu", "notes_enc": None,
         "source_day_id": None},
    ]  # fmt: skip


def _blocks(default1: uuid.UUID | None = O1, swap2: bool = True) -> list[dict[str, Any]]:
    return [
        {"option_block_id": B1, "proposal_day_id": D1, "sort_order": 0, "title": "Lodging", "type": "lodging",
         "client_may_swap": True, "default_option_id": default1, "default_pinned": False, "source_block_id": None},
        {"option_block_id": B2, "proposal_day_id": D2, "sort_order": 0, "title": "Activity", "type": "activity",
         "client_may_swap": swap2, "default_option_id": O3, "default_pinned": False, "source_block_id": None},
    ]  # fmt: skip


def _opt(oid: uuid.UUID, block: uuid.UUID, title: str, sell: str, **over: Any) -> dict[str, Any]:
    row = {
        "option_id": oid, "option_block_id": block, "sort_order": 0, "source": "agency", "title": title,
        "tier": None, "agency_supplier_id": None, "sell_amount": Decimal(sell), "net_amount": Decimal(sell) / 2,
        "pay_on_arrival": False, "choose_later_allowed": False, "availability": "agency",
        "availability_checked_at": None, "aa_tour_source_id": None, "aa_component_id": None,
        "aa_component_kind": None, "aa_departure_id": None, "aa_nights": None, "aa_quote_id": None,
        "aa_quote_valid_until": None,
        "aa_from_price": None, "below_from_price": None, "source_option_id": None, "aa_served_state": None,
    }  # fmt: skip
    row.update(over)
    return row


def _options() -> list[dict[str, Any]]:
    return [
        _opt(O1, B1, "Homestay", "200", tier="comfort"),
        _opt(O2, B1, "Lodge", "300", tier="premium", source="aa", availability="instant", aa_tour_source_id="T-1",
             aa_component_id="tp:1", aa_component_kind="trip_plan", aa_quote_id="q1",
             aa_quote_valid_until=NOW + dt.timedelta(days=1), aa_from_price=Decimal("200"), below_from_price=False,
             source_option_id=uuid.uuid4()),
        _opt(O3, B2, "Rafting", "100"),
    ]  # fmt: skip


def _conn(
    *,
    proposal: dict[str, Any] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    options: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
    picks: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.proposal p JOIN crm.trip t", proposal if proposal is not None else _proposal())
    conn.on("FROM crm.proposal_day", _days())
    conn.on("FROM crm.option_block WHERE proposal_id", blocks if blocks is not None else _blocks())
    conn.on("FROM crm.option o", options if options is not None else _options())
    conn.on("FROM crm.option_rule WHERE proposal_id", rules or [])
    conn.on("FROM crm.selection s WHERE", picks or [])
    conn.on("edit_seq = edit_seq + 1, total_amount", 6)
    for fragment, answer in extra.items():
        conn.on(fragment.replace("_", " ") if fragment.startswith("sql ") else fragment, answer)
    return conn


async def _run(fn: Any, *args: Any, conn: ScriptedConn | None = None, role: str = "agent", **kw: Any) -> Any:
    return await fn(tenant_ctx(conn or _conn(), role=role), FakeVault(), *args, **kw)


async def test_the_lock_and_its_version_conflict() -> None:
    for proposal, code in ((None, "proposal_not_found"), (_proposal(kind="text"), "proposal_not_builder"),
                           (_proposal(status="published"), "proposal_not_draft")):  # fmt: skip
        conn = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip t", proposal)
        with pytest.raises((NotFound, Conflict)) as err:
            await _run(builder.update_settings, P, 5, {}, conn=conn)
        assert err.value.code == code
    with pytest.raises(Conflict) as err:
        await _run(builder.update_settings, P, 4, {})
    assert err.value.code == "version_conflict" and err.value.extra == {"reason": "edited", "current_seq": 5}


async def test_the_view_builds_margin_for_owner_and_agent_only() -> None:
    v = await _run(builder.view, P, role="owner")
    assert v["days"][1]["date"] == "2026-12-01" and v["days"][1]["utc_offset"] == "+05:45"
    assert v["days"][1]["notes"] == "Welcome" and v["intro"] == "Hello"
    o = v["days"][1]["blocks"][0]["options"]
    assert o[0]["is_default"] and o[1]["sell_delta"] == "100.00" and o[1]["net_delta"] == "50.00"
    assert o[1]["aa"]["from_price"] == "200.00" and not o[1]["aa"]["quote_expired"]
    assert v["totals"] == {"currency": "USD", "sell_total": "300.00", "pay_on_arrival_total": "0.00",
                           "deposit": "90.00", "balance": "210.00", "net_total": "150.00",
                           "margin_total": "150.00"}  # fmt: skip
    assert v["checks"] == {"publishable": True, "blocking": [], "warnings": []}
    seen = await _run(builder.view, P, role="assistant")
    assert "net_total" not in seen["totals"] and "net_amount" not in seen["days"][1]["blocks"][0]["options"][0]
    with pytest.raises(Conflict):
        await _run(builder.view, P, conn=ScriptedConn().on("FROM crm.proposal p", _proposal(kind="text")))
    with pytest.raises(NotFound):
        await _run(builder.view, P, conn=ScriptedConn())


async def test_checks_list_what_blocks_publish() -> None:
    options = _options()
    options[0]["availability"] = "sold_out"
    options[1]["aa_quote_valid_until"] = NOW - dt.timedelta(hours=1)
    options[1]["below_from_price"] = True
    options[1]["aa_served_state"] = "retired"
    options[2]["net_amount"] = None
    blocks = _blocks()
    blocks[1]["default_option_id"] = None
    v = await _run(builder.view, P, conn=_conn(options=options, blocks=blocks,
                                               proposal=_proposal(balance_due_on=None)))  # fmt: skip
    assert v["checks"]["blocking"] == ["aa_prices_stale", "balance_due_on_required", "block_without_default",
                                       "default_sold_out"]  # fmt: skip
    assert v["checks"]["warnings"] == ["aa_no_longer_offered", "below_from_price", "net_missing"]
    empty = await _run(builder.view, P, conn=_conn(options=[], blocks=[]))
    assert empty["checks"]["blocking"] == ["no_options", "nothing_to_charge"]
    # The deposit covers the total (100 %): a balance date would break proposal_amounts_chk at publish (no 500).
    full = await _run(builder.view, P, conn=_conn(proposal=_proposal(deposit_pct=Decimal("100"))))
    assert full["checks"]["blocking"] == ["balance_due_on_not_needed"]
    none_left = await _run(builder.view, P, conn=_conn(proposal=_proposal(deposit_pct=Decimal("100"),
                                                                           balance_due_on=None)))  # fmt: skip
    assert none_left["checks"]["publishable"]
    with pytest.raises(Invalid) as err:
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None,
                   conn=_conn(proposal=_proposal(deposit_pct=Decimal("100"))))  # fmt: skip
    assert err.value.extra["blocking"] == ["balance_due_on_not_needed"]


async def test_a_sold_out_aa_alternative_with_an_expired_quote_does_not_block_publish() -> None:
    """Refresh marks a sold-out AA item without a new quote, so its old quote stays expired; a non-default one is
    never charged, so it neither blocks Publish (aa_prices_stale) nor shows as an expired quote. A live expired one
    still blocks."""
    options = _options()  # O2: the AA alternative in block 1 (O1 is the default)
    options[1].update(availability="sold_out", aa_quote_valid_until=NOW - dt.timedelta(hours=1))
    v = await _run(builder.view, P, conn=_conn(options=options), role="owner")
    aa = v["days"][1]["blocks"][0]["options"][1]
    assert aa["availability"] == "sold_out" and not aa["aa"]["quote_expired"]
    assert v["checks"] == {"publishable": True, "blocking": [], "warnings": []}
    options[1]["availability"] = "instant"
    live = await _run(builder.view, P, conn=_conn(options=options), role="owner")
    assert live["days"][1]["blocks"][0]["options"][1]["aa"]["quote_expired"]
    assert live["checks"]["blocking"] == ["aa_prices_stale"]


async def test_an_aa_item_without_a_sell_never_shows_its_b2b_price() -> None:
    """Design §4.6: an AA item added without a sell (the assistant cannot send one) keeps sell 0, so nothing she reads
    carries the b2b price; Publish waits for an owner / agent to price it (aa_item_unpriced)."""
    options = _options()
    options[1].update(sell_amount=Decimal("0"), net_amount=Decimal("987.65"), below_from_price=None)
    for role in ("assistant", "owner"):
        v = await _run(builder.view, P, conn=_conn(options=options), role=role)
        aa_item = v["days"][1]["blocks"][0]["options"][1]
        assert aa_item["aa"]["unpriced"] and aa_item["sell_amount"] == "0.00"
        assert v["checks"]["blocking"] == ["aa_item_unpriced"]
        if role == "assistant":
            assert "987.65" not in json.dumps(v) and "net_amount" not in aa_item


async def test_create_a_skeleton_or_a_copy() -> None:
    trip = {"trip_id": TRIP, "stage": "proposal", "start_date": START, "end_date": START + dt.timedelta(days=2),
            "nights": None, "destinations": ["nepal"], "party_adults": 2, "party_child_ages": [],
            "party_type": "couple"}  # fmt: skip
    conn = _conn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    conn.on("coalesce(max(version), 0) + 1", 2)
    v = await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=conn)
    assert v["kind"] == "builder" and v["status"] == "draft"
    assert len(conn.sql("INSERT INTO crm.proposal_day")) == 4  # day 0 + three dates
    assert len(conn.sql("INSERT INTO crm.option_block")) == 2  # two nights
    assert conn.sql("INSERT INTO crm.proposal (")[0][6] is None  # no balance date yet
    with pytest.raises(Invalid):
        await _run(builder.create, TRIP, currency="usd", from_proposal_id=None, conn=conn)
    # A working copy carries the lineage and the pick counter of its source.
    rules = [{"option_rule_id": R1, "kind": "excludes", "option_id": O2, "target_option_id": O3}]
    conn = _conn(rules=rules).on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    conn.on("coalesce(max(version), 0) + 1", 3)
    await _run(builder.create, TRIP, currency=None, from_proposal_id=BASE, conn=conn)
    assert len(conn.sql("INSERT INTO crm.option (")) == 3 and len(conn.sql("INSERT INTO crm.option_rule")) == 1
    assert conn.sql("INSERT INTO crm.proposal_day")[1][-1] == D1  # source_day_id
    for found, code in ((None, "trip_not_found"), ({**trip, "stage": "booked"}, "trip_not_open_for_proposal")):
        c = ScriptedConn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", found)
        with pytest.raises((NotFound, Conflict)) as err:
            await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=c)
        assert err.value.code == code
    c = ScriptedConn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    c.on("status = 'accepted'", True)
    with pytest.raises(Conflict) as err:
        await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=c)
    assert err.value.code == "proposal_already_accepted"
    c = ScriptedConn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    c.on("kind = 'builder' AND status = 'draft'", P)
    with pytest.raises(Conflict) as err:
        await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=c)
    assert err.value.code == "builder_draft_exists"
    c = ScriptedConn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    c.on("coalesce(max(version), 0) + 1", 1000)
    with pytest.raises(Conflict):
        await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=c)
    c = _conn(proposal=_proposal(kind="text")).on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL", trip)
    c.on("coalesce(max(version), 0) + 1", 3)
    with pytest.raises(Conflict) as err:
        await _run(builder.create, TRIP, currency=None, from_proposal_id=BASE, conn=c)
    assert err.value.code == "proposal_not_builder"


async def test_settings() -> None:
    conn = _conn().on("FROM crm.agreement_doc", {"doc_id": BASE, "status": "draft", "body_sha256": b"x"})
    await _run(builder.update_settings, P, 5, {"price_visibility": "total_only", "deposit_pct": Decimal("50"),
               "room_config": [{"type": "single", "adults": 1}], "intro": "Hi", "terms_doc_id": BASE},
               conn=conn)  # fmt: skip
    args = conn.sql("UPDATE crm.proposal SET price_visibility")[0]
    assert args[1] == "total_only" and args[3] == Decimal("50") and b"Hi" in args[7]
    for change, code in (({"price_visibility": "per_line"}, "invalid_price_visibility"),
                         ({"deposit_pct": None}, "invalid_deposit_pct"),
                         ({"room_config": None}, "invalid_room_config"),
                         ({"room_config": [{"type": "suite", "adults": 1}]}, "invalid_room_config"),
                         ({"deposit_pct": Decimal("0")}, "invalid_deposit_pct"),
                         ({"intro": "x" * 20_001}, "body_too_long")):  # fmt: skip
        with pytest.raises(Invalid) as err:
            await _run(builder.update_settings, P, 5, change)
        assert err.value.code == code
    with pytest.raises(NotFound):
        await _run(builder.update_settings, P, 5, {"terms_doc_id": BASE})


async def test_days() -> None:
    conn = _conn()
    await _run(builder.add_day, P, 5, {"day_no": 1, "title": " Pokhara  lake ", "notes": "Boat\r\nride"}, conn=conn)
    assert conn.sql("SET day_no = day_no + $3")[0] == (P, 1, 1)
    inserted = conn.sql("INSERT INTO crm.proposal_day")[0]
    assert inserted[4] == "Pokhara lake" and inserted[6] == "Asia/Kathmandu"
    for body, code in (({"day_no": 5}, "invalid_day_no"), ({"tz": "UTC+7"}, "invalid_tz"),
                       ({"title": "x" * 121}, "title_too_long"), ({"notes": 3}, "invalid_notes"),
                       ({"notes": "x" * 4001}, "notes_too_long"), ({"title": 3}, "invalid_title")):  # fmt: skip
        with pytest.raises(Invalid) as err:
            await _run(builder.add_day, P, 5, body)
        assert err.value.code == code
    await _run(builder.update_day, P, 5, D1, {"title": None, "tz": "Asia/Kolkata", "notes": ""})
    with pytest.raises(NotFound):
        await _run(builder.update_day, P, 5, uuid.uuid4(), {})
    conn = _conn()
    await _run(builder.delete_day, P, 5, D1, conn=conn)
    assert conn.sql("DELETE FROM crm.option WHERE option_block_id")[0] == ([B1],)
    with pytest.raises(Conflict) as err:
        await _run(builder.delete_day, P, 5, D0)
    assert err.value.code == "before_you_go_kept"
    conn = _conn()
    conn.on("FROM crm.proposal_day", _days()[:2])
    with pytest.raises(Conflict) as err:
        await _run(builder.delete_day, P, 5, D1, conn=conn)
    assert err.value.code == "last_day_kept"
    conn = _conn()
    await _run(builder.reorder_days, P, 5, [D2, D1], conn=conn)
    assert conn.sql("unnest($2::uuid[])")[0] == (P, [D2, D1])
    with pytest.raises(Invalid):
        await _run(builder.reorder_days, P, 5, [D2, D2])
    many = [{**_days()[1], "proposal_day_id": uuid.uuid4(), "day_no": n} for n in range(1, 61)]
    with pytest.raises(Conflict):
        await _run(builder.add_day, P, 5, {}, conn=_conn().on("FROM crm.proposal_day", many))


async def test_blocks() -> None:
    conn = _conn()
    await _run(builder.add_block, P, 5, D2, {"title": "Dinner", "type": "meal"}, conn=conn)
    assert conn.sql("INSERT INTO crm.option_block")[0][4] == 1  # after the day's last block
    with pytest.raises(Invalid):
        await _run(builder.add_block, P, 5, D2, {"title": "x", "type": "spa"})
    with pytest.raises(Invalid):
        await _run(builder.add_block, P, 5, D2, {"title": "  "})
    full = [{**_blocks()[1], "option_block_id": uuid.uuid4()} for _ in range(12)]
    with pytest.raises(Conflict):
        await _run(builder.add_block, P, 5, D2, {"title": "x"}, conn=_conn(blocks=full))
    conn = _conn()
    await _run(builder.update_block, P, 5, B1, {"default_option_id": O2, "client_may_swap": False, "title": "Stay",
                                               "type": "lodging", "sort_order": 3}, conn=conn)  # fmt: skip
    assert conn.sql("UPDATE crm.option_block SET title")[0] == (B1, "Stay", "lodging", False, O2, 3, True)  # pinned
    with pytest.raises(Invalid) as err:
        await _run(builder.update_block, P, 5, B1, {"default_option_id": O3})
    assert err.value.code == "invalid_default_option"
    sold = _options()
    sold[1]["availability"] = "sold_out"
    with pytest.raises(Conflict):
        await _run(builder.update_block, P, 5, B1, {"default_option_id": O2}, conn=_conn(options=sold))
    with pytest.raises(Invalid):
        await _run(builder.update_block, P, 5, B1, {"sort_order": 100})
    with pytest.raises(NotFound):
        await _run(builder.update_block, P, 5, uuid.uuid4(), {})
    rules = [{"option_rule_id": R1, "kind": "excludes", "option_id": O1, "target_option_id": O3}]
    with pytest.raises(Invalid) as err:  # card R: the defaults now break a rule
        await _run(builder.update_block, P, 5, B1, {"title": "x"}, conn=_conn(rules=rules))
    assert err.value.code == "option_conflict" and "Homestay excludes Rafting" in err.value.message
    conn = _conn()
    await _run(builder.delete_block, P, 5, B2, conn=conn)
    assert conn.sql("DELETE FROM crm.option_block")[0] == ([B2],)


async def test_options() -> None:
    conn = _conn(blocks=_blocks(default1=None)).on("FROM crm.agency_supplier", {"agency_supplier_id": BASE,
                                                                                "kind": "agency"})  # fmt: skip
    await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp", "sell_amount": Decimal("1.50"),
               "agency_supplier_id": BASE, "tier": "budget"}, conn=conn)  # fmt: skip
    assert conn.sql("SET default_option_id = $2")[0] == (B1, O1, False)  # rule 5 fallback: the first open option
    with pytest.raises(Invalid):
        await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp", "tier": "gold"})
    with pytest.raises(Invalid):
        await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp", "sell_amount": Decimal("1.001")})
    with pytest.raises(NotFound):
        await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp", "agency_supplier_id": BASE})
    aa_supplier = _conn().on("FROM crm.agency_supplier", {"agency_supplier_id": BASE, "kind": "aa"})
    with pytest.raises(Invalid):
        await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp", "agency_supplier_id": BASE},
                   conn=aa_supplier)  # fmt: skip
    full = [_opt(uuid.uuid4(), B1, "x", "1") for _ in range(12)]
    with pytest.raises(Conflict):
        await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp"}, conn=_conn(options=full))
    conn = _conn()
    await _run(builder.update_option, P, 5, O2, {"sell_amount": Decimal("150"), "tier": "comfort"}, conn=conn)
    assert conn.sql("UPDATE crm.option SET title")[0][8] is True  # 150 for two is below the 200 "from" price
    for change, kind in (({"net_amount": Decimal("1")}, Forbidden), ({"choose_later_allowed": True}, Invalid),
                         ({"agency_supplier_id": BASE}, Invalid)):  # fmt: skip
        with pytest.raises(kind):
            await _run(builder.update_option, P, 5, O2, change)
    await _run(builder.update_option, P, 5, O1, {"net_amount": None, "pay_on_arrival": True})
    with pytest.raises(NotFound):
        await _run(builder.update_option, P, 5, uuid.uuid4(), {})
    conn = _conn()
    await _run(builder.update_option, P, 5, O1, {"pay_on_arrival": None, "choose_later_allowed": None}, conn=conn)
    assert conn.sql("UPDATE crm.option SET title")[0][6:8] == (False, False)  # null keeps the current value
    conn = _conn()
    await _run(builder.delete_option, P, 5, O1, conn=conn)
    assert conn.sql("DELETE FROM crm.option WHERE option_id")[0] == (O1,)
    assert any("default_pinned = false" in flat for flat, _ in conn.calls if "SET default_option_id = NULL" in flat)


async def test_rule_5_never_moves_a_default_the_agent_chose(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Stage 2 tier (comfort here) sets a lodging default only until the agent picks one by hand."""
    monkeypatch.setattr(builder, "_tier", _tier_of("comfort"))
    o9 = uuid.UUID(int=9)
    extra = _opt(o9, B1, "Guesthouse", "150", tier="comfort")
    for pinned, moved in ((False, True), (True, False)):
        blocks = _blocks(default1=O2)  # the premium AA lodge is the default
        blocks[0]["default_pinned"] = pinned
        options = [_opt(O2, B1, "Lodge", "300", tier="premium"), extra, _opt(O3, B2, "Rafting", "100")]
        conn = _conn(blocks=blocks, options=options)
        await _run(builder.add_agency_option, P, 5, B1, {"title": "Camp", "tier": "budget"}, conn=conn)
        assert bool(conn.sql("SET default_option_id = $2, default_pinned")) is moved
        if moved:
            assert conn.sql("SET default_option_id = $2, default_pinned")[0] == (B1, o9, False)


def _tier_of(value: str | None) -> Any:
    async def tier(*_args: Any) -> str | None:
        return value

    return tier


async def test_aa_items() -> None:
    tour = {"source_id": "T-1", "title": "Everest view", "country_code": "NP", "duration_days": 3,
            "served_state": "served", "reason": None}  # fmt: skip
    conn = _conn().on("SELECT source_id, title, country_code", tour)
    plan = await builder.aa_plan(tenant_ctx(conn), P, 5, B1, {"tour_source_id": "T-1", "component_id": "tp:1",
                                                              "component_kind": "trip_plan"})  # fmt: skip
    assert plan["service_date"] == "2026-12-01" and plan["pax"]["adult"] == 2 and plan["party"] == 2
    with pytest.raises(Conflict):
        await builder.aa_plan(tenant_ctx(_conn().on("SELECT source_id, title, country_code",
                                                    {**tour, "served_state": "paused"})), P, 5, B1,
                              {"tour_source_id": "T-1"})  # fmt: skip
    with pytest.raises(Invalid):
        await builder.aa_plan(tenant_ctx(_conn(proposal=_proposal(starts_on=None)).on(
            "SELECT source_id, title, country_code", tour)), P, 5, B1, {"tour_source_id": "T-1"})  # fmt: skip
    with pytest.raises(Invalid) as err:  # a hotel room is quoted for its nights (as AA-947 aa_lines)
        await builder.aa_plan(tenant_ctx(_conn().on("SELECT source_id, title, country_code", tour)), P, 5, B1,
                              {"tour_source_id": "T-1", "component_id": "hr:1",
                               "component_kind": "hotel_room"})  # fmt: skip
    assert err.value.code == "invalid_nights"
    quote = AaQuote("q9", Decimal("120"), "on_request", NOW, Decimal("100"))
    conn = _conn()
    await _run(builder.add_aa_option, plan, quote, {"sell_amount": None}, conn=conn)
    row = conn.sql("INSERT INTO crm.option (")[0]
    # No sell given: the sell stays 0, never the b2b price (the assistant reads the sell, §4.6); no warning yet.
    assert row[9] == Decimal("0.00") and row[10] == Decimal("120.00") and row[13] == "on_request"
    assert row[22] is None and row[24] is None  # below_from_price, aa_nights
    conn = _conn()
    await _run(builder.add_aa_option, plan, quote, {"sell_amount": Decimal("120")}, conn=conn)
    row = conn.sql("INSERT INTO crm.option (")[0]
    assert row[9] == Decimal("120") and row[22] is True  # 60 a person, below the 100 "from" price (a warning)
    hotel = {**plan, "component_id": "hr:1", "component_kind": "hotel_room", "nights": 3}
    conn = _conn()
    await _run(builder.add_aa_option, hotel, quote, {"sell_amount": Decimal("500")}, conn=conn)
    assert conn.sql("INSERT INTO crm.option (")[0][24] == 3  # the nights are kept for every re-quote
    with pytest.raises(Conflict) as err:
        await _run(builder.add_aa_option, plan, AaQuote("q9", Decimal("1"), "sold_out", NOW, None), {})
    assert err.value.code == "aa_sold_out"
    plans = await builder.aa_refresh_plan(tenant_ctx(_conn()), P, 5)
    assert [p["option_id"] for p in plans] == [str(O2)]
    # The nights go out again, the oldest quote first, a day without a date is listed (refresh reports it).
    o4, o5 = uuid.UUID(int=4), uuid.UUID(int=5)
    aa = dict(source="aa", availability="instant", aa_tour_source_id="T-1", aa_component_kind="hotel_room",
              aa_component_id="hr:1", aa_quote_id="q", aa_nights=3)  # fmt: skip
    many = [*_options(), _opt(o4, B2, "Hotel A", "300", **aa, aa_quote_valid_until=NOW - dt.timedelta(days=2)),
            _opt(o5, B2, "Hotel B", "300", **aa, aa_quote_valid_until=None)]  # fmt: skip
    ordered = await builder.aa_refresh_plan(tenant_ctx(_conn(options=many)), P, 5)
    assert [p["option_id"] for p in ordered] == [str(o5), str(o4), str(O2)]
    assert ordered[1]["nights"] == 3 and ordered[2]["nights"] is None
    undated = await builder.aa_refresh_plan(tenant_ctx(_conn(proposal=_proposal(starts_on=None))), P, 5)
    assert undated[0]["service_date"] is None
    conn = _conn()
    await _run(builder.apply_refresh, P, 5, [(plans[0], AaQuote("q10", Decimal("130"), "available", NOW,
                                                                  Decimal("200"))),
                                             (plans[0], AaQuote(None, None, "sold_out", NOW, None))],
               conn=conn)  # fmt: skip
    assert conn.sql("SET net_amount = $2, availability = $3")[0][1:3] == (Decimal("130.00"), "instant")
    assert conn.sql("SET availability = 'sold_out'")[0] == (O2,)


async def test_rules() -> None:
    conn = _conn()
    await _run(builder.add_rule, P, 5, {"kind": "requires", "option_id": O1, "target_option_id": O3}, conn=conn)
    assert conn.sql("INSERT INTO crm.option_rule")[0][3:] == ("requires", O1, O3)
    for body, code in (({"kind": "maybe", "option_id": O1, "target_option_id": O3}, "invalid_rule_kind"),
                       ({"kind": "excludes", "option_id": O1, "target_option_id": O2}, "rule_same_block")):  # fmt: skip
        with pytest.raises(Invalid) as err:
            await _run(builder.add_rule, P, 5, body)
        assert err.value.code == code
    existing = [{"option_rule_id": R1, "kind": "requires", "option_id": O1, "target_option_id": O3}]
    with pytest.raises(Conflict):
        await _run(builder.add_rule, P, 5, {"kind": "requires", "option_id": O1, "target_option_id": O3},
                   conn=_conn(rules=existing))  # fmt: skip
    with pytest.raises(Conflict):
        await _run(builder.add_rule, P, 5, {"kind": "excludes", "option_id": O1, "target_option_id": O3},
                   conn=_conn(rules=existing * 100))  # fmt: skip
    await _run(builder.delete_rule, P, 5, R1)
    conn = _conn()
    conn.on("DELETE FROM crm.option_rule WHERE proposal_id", None)

    async def none_deleted(sql: str, *args: Any) -> str:
        return "DELETE 0"

    conn.execute = none_deleted  # type: ignore[method-assign]
    with pytest.raises(NotFound):
        await _run(builder.delete_rule, P, 5, R1, conn=conn)


async def test_publish() -> None:
    conn = _conn(proposal=_proposal(terms_doc_id=BASE)).on(
        "FROM crm.agreement_doc", {"doc_id": BASE, "status": "published", "body_sha256": b"\x01" * 32}
    )
    conn.on("SET status = 'superseded'", [{"proposal_id": BASE}])
    conn.on("SET status = 'published'", NOW)
    out = await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=conn)
    assert out["publish"]["superseded"] == [str(BASE)] and len(out["publish"]["price_hash"]) == 64
    assert conn.sql("SET status = 'published'")[0][2:] == (Decimal("300.00"), Decimal("90.00"))
    # The hash covers what the client sees: another sell amount, another hash.
    other = _options()
    other[0]["sell_amount"] = Decimal("201")
    s1 = {"days": _days(), "blocks": _blocks(), "options": _options(), "rules": []}
    s2 = {**s1, "options": other}
    sums = {"sell_total": Decimal(1), "deposit": Decimal(1), "pay_on_arrival_total": Decimal(0)}
    assert builder.price_hash(_proposal(), "x", None, s1, sums) != builder.price_hash(_proposal(), "x", None, s2, sums)
    with pytest.raises(Conflict) as err:
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None,
                   conn=_conn(proposal=_proposal(trip_stage="booked")))  # fmt: skip
    assert err.value.code == "trip_not_open_for_proposal"
    with pytest.raises(Invalid) as err:
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=_conn(options=[], blocks=[]))
    assert err.value.code == "not_publishable" and err.value.extra["blocking"] == ["no_options", "nothing_to_charge"]
    rules = [{"option_rule_id": R1, "kind": "requires", "option_id": O1, "target_option_id": O2}]
    with pytest.raises(Invalid) as err:
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=_conn(rules=rules))
    assert err.value.code == "option_conflict"
    unpublished = _conn(proposal=_proposal(terms_doc_id=BASE)).on("FROM crm.agreement_doc", None)
    with pytest.raises(Conflict):
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=unpublished)


CURRENT_TERMS = "kind = 'booking_terms' AND status = 'published'"


async def test_a_version_without_terms_is_pinned_to_the_published_terms() -> None:
    """S261 live finding (AA-952 x AA-955): a builder version saved terms_doc_id null, so the client accepted no terms
    and the deposit then waited for a re-accept. The skeleton starts with the agency's published booking terms and
    publish pins them to a version without terms, before the price hash (design v3 F6 step 3, WF-7, R34)."""
    terms_id = uuid.uuid4()
    current = {"doc_id": terms_id, "version_label": "v2026-10", "body_text": "Terms", "body_sha256": b"\x02" * 32,
               "published_at": NOW}  # fmt: skip
    trip = {"trip_id": TRIP, "stage": "proposal", "start_date": START, "end_date": START + dt.timedelta(days=1),
            "nights": None, "destinations": ["nepal"], "party_adults": 2, "party_child_ages": [],
            "party_type": "couple"}  # fmt: skip
    conn = _conn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    conn.on("coalesce(max(version), 0) + 1", 2).on(CURRENT_TERMS, current)
    await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=conn)
    assert conn.sql("INSERT INTO crm.proposal (")[0][6] == terms_id
    # No published terms: the skeleton has none (and publish pins nothing).
    conn = _conn().on("FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE", trip)
    conn.on("coalesce(max(version), 0) + 1", 2)
    await _run(builder.create, TRIP, currency="USD", from_proposal_id=None, conn=conn)
    assert conn.sql("INSERT INTO crm.proposal (")[0][6] is None
    # Publish: a version without terms takes the published version; the hash covers it.
    conn = (
        _conn()
        .on(CURRENT_TERMS, current)
        .on(
            "FROM crm.agreement_doc WHERE doc_id",
            {"doc_id": terms_id, "status": "published", "body_sha256": b"\x02" * 32},
        )
    )
    conn.on("SET status = 'published'", NOW)
    out = await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=conn)
    assert conn.sql("SET terms_doc_id = $2 WHERE proposal_id = $1 AND terms_doc_id IS NULL") == [(P, terms_id)]
    s = {"days": _days(), "blocks": _blocks(), "options": _options(), "rules": []}
    sums = builder._charged(s, None, Decimal("30"))
    pinned = builder.price_hash(_proposal(terms_doc_id=terms_id), "Hello", b"\x02" * 32, s, sums)
    assert out["publish"]["price_hash"] == pinned.hex()
    assert out["publish"]["price_hash"] != builder.price_hash(_proposal(), "Hello", None, s, sums).hex()
    # A version that names its terms keeps them; no published terms: nothing is pinned.
    for conn in (
        _conn(proposal=_proposal(terms_doc_id=BASE))
        .on(CURRENT_TERMS, current)
        .on(
            "FROM crm.agreement_doc WHERE doc_id", {"doc_id": BASE, "status": "published", "body_sha256": b"\x01" * 32}
        ),
        _conn(),
    ):
        conn.on("SET status = 'published'", NOW)
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=conn)
        assert not conn.sql("SET terms_doc_id = $2")


async def test_publish_surfaces_client_picks_and_carries_them() -> None:
    base = {"proposal_id": BASE, "version": 1, "status": "sent", "selection_seq": 2}
    options = _options()
    source = options[1]["source_option_id"]
    conn = _conn(proposal=_proposal(based_on_proposal_id=BASE, based_on_selection_seq=1), options=options)
    conn.on("SELECT proposal_id, version, status, selection_seq FROM crm.proposal", base)
    conn.on("JOIN crm.option_block b ON", [{"block_title": "Lodging", "option_title": "Lodge", "chosen_by": "client",
                                            "updated_at": NOW}])  # fmt: skip
    with pytest.raises(Conflict) as err:
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=conn)
    assert err.value.extra["reason"] == "client_selected" and err.value.extra["picks"][0]["option"] == "Lodge"
    conn.on("FROM crm.selection s WHERE",
            [{"option_block_id": uuid.uuid4(), "option_id": source, "chosen_by": "client", "choose_later": False,
              "updated_at": NOW},
             {"option_block_id": uuid.uuid4(), "option_id": uuid.uuid4(), "chosen_by": "client",
              "choose_later": False, "updated_at": NOW}])  # fmt: skip
    conn.on("SET status = 'published'", NOW)
    out = await _run(builder.publish, P, 5, acknowledge_selection_seq=2, conn=conn)
    assert out["publish"]["picks_carried"] == 1
    assert conn.sql("INSERT INTO crm.selection")[0][2:5] == (B1, O2, "carried")


async def test_picks() -> None:
    published = _proposal(status="published")
    conn = _conn(proposal=published).on("selection_seq = selection_seq + 1", 3)
    out = await builder.record_pick(tenant_ctx(conn), P, B1, O2)
    assert out == {"proposal_id": str(P), "selection_seq": 3, "sell_total": "400.00", "deposit": "120.00"}
    cases = [
        (ScriptedConn(), NotFound, "proposal_not_found", B1, O2, {}),
        (_conn(), Conflict, "proposal_not_open_for_picks", B1, O2, {}),
        (_conn(proposal=published), Invalid, "option_not_in_block", B2, O2, {}),
        (_conn(proposal=published, blocks=_blocks(swap2=False),
               options=[*_options(), _opt(uuid.UUID(int=7), B2, "Hike", "1")]),
         Invalid, "swap_not_allowed", B2, uuid.UUID(int=7), {}),
        (_conn(proposal=published), Invalid, "choose_later_not_allowed", B1, O2, {"choose_later": True}),
        (_conn(proposal=published, rules=[{"option_rule_id": R1, "kind": "excludes", "option_id": O2,
                                           "target_option_id": O3}]), Invalid, "option_conflict", B1, O2, {}),
    ]  # fmt: skip
    for conn, kind, code, block, option, kw in cases:
        with pytest.raises(kind) as err:
            await builder.record_pick(tenant_ctx(conn), P, block, option, **kw)
        assert err.value.code == code
    sold = _options()
    sold[1]["availability"] = "sold_out"
    with pytest.raises(Conflict) as err:
        await builder.record_pick(tenant_ctx(_conn(proposal=published, options=sold)), P, B1, O2)
    assert err.value.code == "option_sold_out"
