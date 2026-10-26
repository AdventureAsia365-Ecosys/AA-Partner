"""AA-949 (card CRM-21a) end to end on PostgreSQL with the RDS role shape: the day-list builder as crm_app through
``tenant_tx()``, with real field encryption and an in-memory Booking (``tests/aa_fakes.py``).

- Spec rule 5 / Fix B10 / R33 / R38: the skeleton follows Stage 1/2 (one day per date, the Before you go day 0, a
  Lodging block per night, Nepal days in Asia/Kathmandu at +05:45 (**card E**), rooms from the party, the lodging
  default from the lead's Stage 2 accommodation tier).
- **Card R**: a requires / excludes rule the defaults break is 422 ``option_conflict`` and nothing is written; a
  client pick that breaks a rule is refused the same way.
- **Card E / §11.2**: two editors on one working copy -> the second write is 409 ``version_conflict``; a client pick on
  the version a working copy came from -> Publish is 409 ``version_conflict`` with the picks, until acknowledged; the
  pick then carries over.
- Publish vN: amounts from the defaults (pay on arrival listed, never charged), price hash, sealed, v(N-1) superseded.
- AA items: the live quote (net = b2b price, availability, "from"-price warning), sold out cannot be added, Booking
  down adds nothing (503), Refresh AA prices; the assistant never sees net or margin.
- Another agency cannot read or change a version (RLS: not found).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import stages
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.intake import completeness
from app.proposals import aa_options, builder, service
from app.tenancy import tenant_tx
from app.travellers import service as travellers
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.test_crm005_intake import _agency, _keys

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _setup(pool: asyncpg.Pool, *, tier: str | None = "comfort") -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent", "assistant")
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    members = {role: people[i][1] for i, role in enumerate(("owner", "agent", "assistant"))}
    keys = _keys()
    trip = uuid.uuid4()
    start = dt.date.today() + dt.timedelta(days=90)
    async with tenant_tx(pool, who["agent"], tenant_id) as ctx:
        vault = Vault(keys, ctx)
        lead = uuid.UUID(
            (await travellers.create_traveler(ctx, vault, {"display_name": "Grace Lee", "email": "g@example.test"}))[
                "traveler_id"
            ]
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip (trip_id, title, owner_member_id, start_date, end_date, destinations, party_adults, "
            "party_child_ages, party_type, intake_completeness) VALUES ($1, 'Nepal family', $2, $3, $4, "
            "'{nepal}', 2, '{8}', 'family', '{\"s1\": 100, \"s2\": 100, \"s3\": 0}')",
            trip,
            members["agent"],
            start,
            start + dt.timedelta(days=4),
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')", trip, lead
        )
        _lead, token = await completeness.lead_token(ctx, vault, trip)
        await ctx.conn.execute(
            "UPDATE crm.traveler_profile SET accommodation_tier = $2 WHERE traveler_token = $1", token, tier
        )
        await stages.move(ctx, trip, "qualifying")
        await stages.move(ctx, trip, "proposal")
    return {"tenant": tenant_id, "who": who, "members": members, "keys": keys, "trip": trip, "start": start}


async def _as(pool: asyncpg.Pool, s: dict[str, Any], role: str, fn: Any, *args: Any, **kw: Any) -> Any:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await fn(ctx, Vault(s["keys"], ctx), *args, **kw)


async def _create(pool: asyncpg.Pool, s: dict[str, Any], role: str = "agent", **kw: Any) -> dict[str, Any]:
    kw.setdefault("currency", "USD")
    kw.setdefault("from_proposal_id", None)
    return await _as(pool, s, role, builder.create, s["trip"], **kw)


def _pid(v: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(v["proposal_id"])


def _block(v: dict[str, Any], day_no: int, index: int = 0) -> dict[str, Any]:
    return next(d for d in v["days"] if d["day_no"] == day_no)["blocks"][index]


async def _option(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], block: dict[str, Any], **body: Any):
    body.setdefault("title", "Homestay")
    return await _as(
        pool, s, "agent", builder.add_agency_option, _pid(v), v["edit_seq"], uuid.UUID(block["option_block_id"]), body
    )


async def test_the_skeleton_follows_stage_1_and_2(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    assert v["kind"] == "builder" and v["status"] == "draft" and v["version"] == 1 and v["edit_seq"] == 2
    assert [d["day_no"] for d in v["days"]] == [0, 1, 2, 3, 4, 5]
    assert v["days"][0]["before_you_go"] and v["days"][0]["date"] is None  # the R38 slot
    assert v["days"][1]["date"] == s["start"].isoformat()
    # Card E: a Nepal day is in Asia/Kathmandu and shows +05:45.
    assert {d["tz"] for d in v["days"]} == {"Asia/Kathmandu"} and v["days"][2]["utc_offset"] == "+05:45"
    # A Lodging block on every night (not on the last day), client may swap by default (Fix E1).
    assert [len(d["blocks"]) for d in v["days"]] == [0, 1, 1, 1, 1, 0]
    assert _block(v, 1)["type"] == "lodging" and _block(v, 1)["client_may_swap"] is True
    # Fix B10: the rooms from the party (2 adults + a child of 8 -> one family room); 30 % deposit; D1 default.
    assert v["room_config"] == [{"type": "family", "adults": 2, "children": 1}]
    assert v["deposit_pct"] == "30.00" and v["price_visibility"] == "total_with_deltas"
    assert v["stage2"]["accommodation_tier"] == "comfort" and v["stage2"]["party_children"] == 1
    assert v["checks"]["blocking"] == ["no_options", "nothing_to_charge"]
    # One working copy per trip.
    with pytest.raises(Conflict) as err:
        await _create(app_pool, s)
    assert err.value.code == "builder_draft_exists" and err.value.extra["proposal_id"] == v["proposal_id"]
    # The text proposal routes leave it alone; the trip's Proposal tab lists it with its kind.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as err:
            await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
        assert err.value.code == "proposal_not_published"  # AA-950: a builder version is sent once published
    overview = await _as(app_pool, s, "agent", service.trip_overview, s["trip"])
    assert overview["proposals"][0]["kind"] == "builder"


async def test_rule_5_the_stage_2_tier_sets_the_lodging_default(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, tier="premium")
    v = await _create(app_pool, s)
    block = _block(v, 1)
    v = await _option(app_pool, s, v, block, title="Guesthouse", tier="comfort", sell_amount=Decimal("100"))
    assert _block(v, 1)["options"][0]["is_default"]  # the first option becomes the default
    v = await _option(app_pool, s, v, block, title="Dwarika's", tier="premium", sell_amount=Decimal("400"))
    options = _block(v, 1)["options"]
    assert [o["is_default"] for o in options] == [False, True]  # the lead asked for premium
    assert options[0]["sell_delta"] == "-300.00" and options[1]["sell_delta"] == "0.00"


async def test_card_r_an_excludes_rule_the_defaults_break_is_refused(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    v = await _option(app_pool, s, v, _block(v, 1), title="Tented camp", sell_amount=Decimal("200"))
    v = await _option(app_pool, s, v, _block(v, 2), title="Jeep transfer", sell_amount=Decimal("80"))
    v = await _option(app_pool, s, v, _block(v, 2), title="Walk in", sell_amount=Decimal("0"))
    camp, jeep = _block(v, 1)["options"][0], _block(v, 2)["options"][0]
    seq = v["edit_seq"]
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.add_rule, _pid(v), seq,
                  {"kind": "excludes", "option_id": uuid.UUID(camp["option_id"]),
                   "target_option_id": uuid.UUID(jeep["option_id"])})  # fmt: skip
    assert err.value.status_code == 422 and err.value.code == "option_conflict"
    assert err.value.extra["violations"][0]["kind"] == "excludes"
    after = await _as(app_pool, s, "agent", builder.view, _pid(v))
    assert after["edit_seq"] == seq and after["rules"] == []  # nothing written
    # The rule fits once the default of day 2 is the walk-in; then making the jeep default again is refused too.
    walk = _block(v, 2)["options"][1]
    v = await _as(app_pool, s, "agent", builder.update_block, _pid(v), seq, uuid.UUID(_block(v, 2)["option_block_id"]),
                  {"default_option_id": uuid.UUID(walk["option_id"])})  # fmt: skip
    v = await _as(app_pool, s, "agent", builder.add_rule, _pid(v), v["edit_seq"],
                  {"kind": "excludes", "option_id": uuid.UUID(camp["option_id"]),
                   "target_option_id": uuid.UUID(jeep["option_id"])})  # fmt: skip
    assert len(v["rules"]) == 1
    with pytest.raises(Invalid) as err:
        await _as(
            app_pool,
            s,
            "agent",
            builder.update_block,
            _pid(v),
            v["edit_seq"],
            uuid.UUID(_block(v, 2)["option_block_id"]),
            {"default_option_id": uuid.UUID(jeep["option_id"])},
        )
    assert err.value.code == "option_conflict"
    # Same block rules are meaningless.
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.add_rule, _pid(v), v["edit_seq"],
                  {"kind": "requires", "option_id": uuid.UUID(jeep["option_id"]),
                   "target_option_id": uuid.UUID(walk["option_id"])})  # fmt: skip
    assert err.value.code == "rule_same_block"


async def _publishable(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    v = await _create(pool, s)
    for day_no in (1, 2, 3, 4):
        v = await _option(pool, s, v, _block(v, day_no), title=f"Lodge {day_no}", sell_amount=Decimal("250"),
                          net_amount=Decimal("150"), tier="comfort")  # fmt: skip
    v = await _option(pool, s, v, _block(v, 1), title=f"Riverside {1}", sell_amount=Decimal("290"),
                      net_amount=Decimal("170"))  # fmt: skip
    day3 = next(d for d in v["days"] if d["day_no"] == 3)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day3["proposal_day_id"]),
                  {"title": "Park entry", "type": "activity", "client_may_swap": False})  # fmt: skip
    v = await _option(pool, s, v, _block(v, 3, 1), title="Chitwan park fee", sell_amount=Decimal("45"),
                      pay_on_arrival=True)  # fmt: skip
    return await _as(pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                     {"balance_due_on": s["start"] - dt.timedelta(days=30)})  # fmt: skip


async def test_publish_vn_fixes_the_amounts_and_seals_the_version(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)
    assert v["checks"] == {"publishable": True, "blocking": [], "warnings": ["net_missing"]}
    assert v["totals"]["sell_total"] == "1000.00" and v["totals"]["pay_on_arrival_total"] == "45.00"
    assert v["totals"]["deposit"] == "300.00" and v["totals"]["net_total"] == "600.00"
    # The assistant prepares drafts but never publishes (the route's PROPOSAL_PUBLISH) and never sees net / margin.
    seen = await _as(app_pool, s, "assistant", builder.view, _pid(v))
    assert "net_total" not in seen["totals"] and "margin_total" not in seen["totals"]
    assert all("net_amount" not in o and "net_delta" not in o for d in seen["days"] for b in d["blocks"]
               for o in b["options"])  # fmt: skip
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published" and len(out["publish"]["price_hash"]) == 64
    assert out["publish"]["superseded"] == [] and out["publish"]["picks_carried"] == 0
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchrow("SELECT total_amount, deposit_amount, status FROM crm.proposal "
                                      "WHERE proposal_id = $1", _pid(v))  # fmt: skip
    assert row["total_amount"] == Decimal("1000.00") and row["deposit_amount"] == Decimal("300.00")
    # Sealed: any write is 409 proposal_not_draft (the database refuses the rows too, CRM35).
    with pytest.raises(Conflict) as err:
        await _option(app_pool, s, out, _block(out, 1), title="Late add")
    assert err.value.code == "proposal_not_draft"
    # A new version from it supersedes it at its publish.
    v2 = await _create(app_pool, s, from_proposal_id=_pid(v))
    assert v2["version"] == 2 and v2["based_on"]["version"] == 1 and v2["based_on"]["client_picks_since_copy"] == 0
    assert v2["totals"]["sell_total"] == "1000.00" and len(v2["days"]) == 6
    out2 = await _as(app_pool, s, "agent", builder.publish, _pid(v2), v2["edit_seq"], acknowledge_selection_seq=None)
    assert out2["publish"]["superseded"] == [v["proposal_id"]]


async def test_card_e_concurrent_edits_and_client_picks_surface_a_version_conflict(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)
    seq = v["edit_seq"]
    # Two editors on one working copy: the second write, made from the same page, is refused.
    await _as(app_pool, s, "agent", builder.update_settings, _pid(v), seq, {"price_visibility": "total_only"})
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "owner", builder.update_settings, _pid(v), seq, {"deposit_pct": Decimal("50")})
    assert err.value.code == "version_conflict" and err.value.extra == {"reason": "edited", "current_seq": seq + 1}
    v = await _as(app_pool, s, "agent", builder.view, _pid(v))
    v1 = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    # The agent starts v2 while the client still looks at v1 ...
    v2 = await _create(app_pool, s, from_proposal_id=_pid(v1))
    v2 = await _as(app_pool, s, "agent", builder.update_day, _pid(v2), v2["edit_seq"],
                   uuid.UUID(v2["days"][1]["proposal_day_id"]), {"title": "Kathmandu arrival"})  # fmt: skip
    # ... and the client swaps day 1 to the riverside lodge on v1 (the CRM-21b page calls record_pick).
    riverside = next(o for o in _block(v1, 1)["options"] if o["title"] == "Riverside 1")
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        picked = await builder.record_pick(ctx, _pid(v1), uuid.UUID(_block(v1, 1)["option_block_id"]),
                                           uuid.UUID(riverside["option_id"]), chosen_by="client")  # fmt: skip
    assert picked["selection_seq"] == 1 and picked["sell_total"] == "1040.00"
    shown = await _as(app_pool, s, "agent", builder.view, _pid(v2))
    assert shown["based_on"]["client_picks_since_copy"] == 1
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", builder.publish, _pid(v2), v2["edit_seq"], acknowledge_selection_seq=None)
    assert err.value.code == "version_conflict" and err.value.extra["reason"] == "client_selected"
    assert err.value.extra["picks"][0]["option"] == "Riverside 1" and err.value.extra["selection_seq"] == 1
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v2), v2["edit_seq"], acknowledge_selection_seq=1)
    assert out["status"] == "published" and out["publish"]["picks_carried"] == 1
    assert out["picks"][0]["chosen_by"] == "carried" and out["totals"]["sell_total"] == "1040.00"


async def test_picks_follow_the_block_and_the_rules(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)
    day1, fee = _block(v, 1), _block(v, 3, 1)
    riverside = next(o for o in day1["options"] if o["title"] == "Riverside 1")
    lodge3 = _block(v, 3)["options"][0]
    # Choosing the riverside lodge on day 1 excludes lodge 3 (the day 3 default); the park fee block is fixed.
    v = await _option(app_pool, s, v, fee, title="Park fee with guide", sell_amount=Decimal("60"), pay_on_arrival=True)
    guided = _block(v, 3, 1)["options"][1]
    v = await _as(app_pool, s, "agent", builder.add_rule, _pid(v), v["edit_seq"],
                  {"kind": "excludes", "option_id": uuid.UUID(riverside["option_id"]),
                   "target_option_id": uuid.UUID(lodge3["option_id"])})  # fmt: skip
    pub = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Invalid) as err:
            await builder.record_pick(ctx, _pid(pub), uuid.UUID(day1["option_block_id"]),
                                      uuid.UUID(riverside["option_id"]))  # fmt: skip
        assert err.value.code == "option_conflict"
        with pytest.raises(Invalid) as err:  # a fixed block keeps its default
            await builder.record_pick(ctx, _pid(pub), uuid.UUID(fee["option_block_id"]),
                                      uuid.UUID(guided["option_id"]))  # fmt: skip
        assert err.value.code == "swap_not_allowed"
    # A draft takes no picks.
    v2 = await _create(app_pool, s, from_proposal_id=_pid(pub))
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as cerr:
            await builder.record_pick(ctx, _pid(v2), uuid.UUID(_block(v2, 1)["option_block_id"]),
                                      uuid.UUID(_block(v2, 1)["options"][0]["option_id"]))  # fmt: skip
        assert cerr.value.code == "proposal_not_open_for_picks"


async def _aa(pool: asyncpg.Pool, s: dict[str, Any], fake: FakeBooking, v: dict[str, Any], role: str = "agent",
              **body: Any) -> dict[str, Any]:  # fmt: skip
    body = {"tour_source_id": "T-949", "component_id": "tp:1", "component_kind": "trip_plan", **body}
    block = uuid.UUID(_block(v, 1)["option_block_id"])
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        plan = await builder.aa_plan(ctx, _pid(v), v["edit_seq"], block, body)
    quote = await aa_options.quote(booking_client(fake), s["tenant"], plan)
    return await _as(pool, s, role, builder.add_aa_option, plan, quote, body)


async def test_aa_items_come_from_a_live_quote(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    # Its own catalog row (the AA lines tests retire T-100 and leave the shared sync state in backoff).
    async with app_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ('T-949', 'Everest view 3 days', 'NP', 3, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'"
        )
    fake = FakeBooking(b2b_price="900.00", availability="on_request")
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, fake, v, sell_amount=Decimal("1500"))
    o = _block(v, 1)["options"][0]
    assert o["source"] == "aa" and o["title"] == "Everest view 3 days" and o["availability"] == "on_request"
    assert o["net_amount"] == "900.00" and o["sell_amount"] == "1500.00" and o["is_default"]
    # "From" 800 per person; 1500 for 3 people is 500 each: a warning, never a block.
    assert o["aa"]["from_price"] == "800.00" and o["aa"]["below_from_price"] is True
    assert "below_from_price" in v["checks"]["warnings"]
    assert fake.calls["POST quotes"] == 1
    # Refresh AA prices: Booking's new price lands on the net, the sell stays the agent's.
    fake.b2b_price = "950.00"
    plans = await _as_plain(app_pool, s, builder.aa_refresh_plan, _pid(v), v["edit_seq"])
    answers, failed = await aa_options.refresh(booking_client(fake), s["tenant"], plans)
    v = await _as(app_pool, s, "agent", builder.apply_refresh, _pid(v), v["edit_seq"], answers)
    o = _block(v, 1)["options"][0]
    assert failed == [] and o["net_amount"] == "950.00" and o["sell_amount"] == "1500.00"
    # Sold out cannot be added; Booking down adds nothing and says so.
    fake.availability = "sold_out"
    with pytest.raises(Conflict) as err:
        await _aa(app_pool, s, fake, v)
    assert err.value.code == "aa_sold_out"
    fake.availability, fake.down = "available", True
    with pytest.raises(Unavailable) as down:
        await _aa(app_pool, s, fake, v)
    assert down.value.code == "booking_unavailable"
    after = await _as(app_pool, s, "agent", builder.view, _pid(v))
    assert len(_block(after, 1)["options"]) == 1 and after["edit_seq"] == v["edit_seq"]
    # An AA item's net is never typed.
    with pytest.raises(Exception) as typed:
        await _as(app_pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"], uuid.UUID(o["option_id"]),
                  {"net_amount": Decimal("1")})  # fmt: skip
    assert getattr(typed.value, "code", "") == "aa_net_from_quote"


async def _catalog_tour(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ('T-949', 'Everest view 3 days', 'NP', 3, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'"
        )


async def test_an_aa_item_added_without_a_sell_never_shows_the_b2b_price(app_pool: asyncpg.Pool) -> None:
    """Design §4.6: the assistant adds AA items (no sell: she may not send one), and nothing she reads carries the
    b2b price (conf:commercial); Publish waits until an owner / agent prices the item (aa_item_unpriced)."""
    s = await _setup(app_pool)
    await _catalog_tour(app_pool)
    fake = FakeBooking(b2b_price="987.65")
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, fake, v, role="assistant")
    seen = await _as(app_pool, s, "assistant", builder.view, _pid(v))
    o = _block(seen, 1)["options"][0]
    assert o["sell_amount"] == "0.00" and o["aa"]["unpriced"] and seen["totals"]["sell_total"] == "0.00"
    assert "987.65" not in json.dumps(v) and "987.65" not in json.dumps(seen)
    assert "aa_item_unpriced" in seen["checks"]["blocking"]
    # The owner sees the net, prices the item, and the check clears.
    owner = await _as(app_pool, s, "owner", builder.view, _pid(v))
    assert _block(owner, 1)["options"][0]["net_amount"] == "987.65"
    v = await _as(app_pool, s, "owner", builder.update_option, _pid(v), owner["edit_seq"], uuid.UUID(o["option_id"]),
                  {"sell_amount": Decimal("1400")})  # fmt: skip
    assert "aa_item_unpriced" not in v["checks"]["blocking"]


async def test_a_hotel_room_is_re_quoted_for_its_nights(app_pool: asyncpg.Pool) -> None:
    """Refresh AA prices sends the nights an item was quoted for: a 3-night room keeps a 3-night net (Booking prices
    per night here), and a hotel room without nights is refused as in AA-947 (invalid_nights)."""
    s = await _setup(app_pool)
    await _catalog_tour(app_pool)
    fake = FakeBooking(b2b_price="100.00", per_night=True)
    v = await _create(app_pool, s)
    with pytest.raises(Invalid) as err:
        await _aa(app_pool, s, fake, v, component_id="hr:1", component_kind="hotel_room")
    assert err.value.code == "invalid_nights" and fake.calls["POST quotes"] == 0
    v = await _aa(app_pool, s, fake, v, component_id="hr:1", component_kind="hotel_room", nights=3,
                  sell_amount=Decimal("420"))  # fmt: skip
    o = _block(v, 1)["options"][0]
    assert o["net_amount"] == "300.00" and o["aa"]["nights"] == 3
    plans = await _as_plain(app_pool, s, builder.aa_refresh_plan, _pid(v), v["edit_seq"])
    answers, failed = await aa_options.refresh(booking_client(fake), s["tenant"], plans)
    v = await _as(app_pool, s, "agent", builder.apply_refresh, _pid(v), v["edit_seq"], answers)
    o = _block(v, 1)["options"][0]
    assert failed == [] and o["net_amount"] == "300.00" and o["sell_amount"] == "420.00"


async def test_a_sold_out_aa_alternative_with_an_expired_quote_never_blocks_publish(app_pool: asyncpg.Pool) -> None:
    """Review finding: refresh marks a sold-out AA item without a new quote, so its old quote expires for good. When
    the item is not the default it is never charged (it cannot be the default or a client pick): Publish stays open
    (no aa_prices_stale, no quote_expired flag), and the item still shows as sold out."""
    s = await _setup(app_pool)
    await _catalog_tour(app_pool)
    fake = FakeBooking(b2b_price="900.00")
    v = await _publishable(app_pool, s)
    v = await _aa(app_pool, s, fake, v, sell_amount=Decimal("1500"))
    aa = next(o for o in _block(v, 1)["options"] if o["source"] == "aa")
    assert not aa["is_default"]
    fake.availability = "sold_out"
    plans = await _as_plain(app_pool, s, builder.aa_refresh_plan, _pid(v), v["edit_seq"])
    answers, failed = await aa_options.refresh(booking_client(fake), s["tenant"], plans)
    v = await _as(app_pool, s, "agent", builder.apply_refresh, _pid(v), v["edit_seq"], answers)
    assert failed == []
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:  # the old quote runs out
        await ctx.conn.execute("UPDATE crm.option SET aa_quote_valid_until = now() - interval '1 hour' "
                               "WHERE option_id = $1", uuid.UUID(aa["option_id"]))  # fmt: skip
    v = await _as(app_pool, s, "agent", builder.view, _pid(v))
    aa = next(o for o in _block(v, 1)["options"] if o["source"] == "aa")
    assert aa["availability"] == "sold_out" and not aa["aa"]["quote_expired"]
    assert v["checks"]["publishable"] and "aa_prices_stale" not in v["checks"]["blocking"]
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published"


async def test_a_full_deposit_with_a_balance_date_is_a_check_not_a_500(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)  # has a balance date
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                  {"deposit_pct": Decimal("100")})  # fmt: skip
    assert v["checks"]["blocking"] == ["balance_due_on_not_needed"]
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert err.value.code == "not_publishable" and err.value.extra["blocking"] == ["balance_due_on_not_needed"]
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"], {"balance_due_on": None})
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published" and out["totals"]["deposit"] == out["totals"]["sell_total"] == "1000.00"


async def test_rule_5_never_moves_a_default_the_agent_chose(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool, tier="comfort")
    v = await _create(app_pool, s)
    block = _block(v, 1)
    v = await _option(app_pool, s, v, block, title="Guesthouse", tier="comfort", sell_amount=Decimal("100"))
    v = await _option(app_pool, s, v, block, title="Dwarika's", tier="premium", sell_amount=Decimal("400"))
    assert [o["is_default"] for o in _block(v, 1)["options"]] == [True, False]  # rule 5: the comfort option
    premium = _block(v, 1)["options"][1]["option_id"]
    v = await _as(app_pool, s, "agent", builder.update_block, _pid(v), v["edit_seq"],
                  uuid.UUID(block["option_block_id"]), {"default_option_id": uuid.UUID(premium)})  # fmt: skip
    assert _block(v, 1)["default_pinned"] is True
    # Adding, editing and deleting other options leaves the agent's choice alone.
    v = await _option(app_pool, s, v, block, title="Comfort inn", tier="comfort", sell_amount=Decimal("120"))
    third = _block(v, 1)["options"][2]["option_id"]
    v = await _as(app_pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"], uuid.UUID(third),
                  {"tier": "comfort"})  # fmt: skip
    v = await _as(app_pool, s, "agent", builder.delete_option, _pid(v), v["edit_seq"], uuid.UUID(third))
    assert _block(v, 1)["default_option_id"] == premium and v["totals"]["sell_total"] == "400.00"
    # Deleting the chosen default hands the block back to rule 5.
    v = await _as(app_pool, s, "agent", builder.delete_option, _pid(v), v["edit_seq"], uuid.UUID(premium))
    assert _block(v, 1)["options"][0]["is_default"] and _block(v, 1)["default_pinned"] is False


async def _as_plain(pool: asyncpg.Pool, s: dict[str, Any], fn: Any, *args: Any) -> Any:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return await fn(ctx, *args)


async def test_days_reorder_delete_and_the_before_you_go_day(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    trip_days = [uuid.UUID(d["proposal_day_id"]) for d in v["days"] if d["day_no"] >= 1]
    v = await _as(app_pool, s, "agent", builder.reorder_days, _pid(v), v["edit_seq"], list(reversed(trip_days)))
    assert [d["proposal_day_id"] for d in v["days"][1:]] == [str(x) for x in reversed(trip_days)]
    v = await _as(app_pool, s, "agent", builder.add_day, _pid(v), v["edit_seq"],
                  {"day_no": 2, "title": "Pokhara", "tz": "Asia/Kathmandu", "notes": "Lakeside walk"})  # fmt: skip
    assert v["days"][2]["title"] == "Pokhara" and v["days"][2]["notes"] == "Lakeside walk"
    assert [d["day_no"] for d in v["days"]] == [0, 1, 2, 3, 4, 5, 6]
    with pytest.raises(Invalid):
        await _as(app_pool, s, "agent", builder.update_day, _pid(v), v["edit_seq"],
                  uuid.UUID(v["days"][1]["proposal_day_id"]), {"tz": "Mars/Base"})  # fmt: skip
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", builder.delete_day, _pid(v), v["edit_seq"],
                  uuid.UUID(v["days"][0]["proposal_day_id"]))  # fmt: skip
    assert err.value.code == "before_you_go_kept"
    v = await _as(app_pool, s, "agent", builder.delete_day, _pid(v), v["edit_seq"],
                  uuid.UUID(v["days"][1]["proposal_day_id"]))  # fmt: skip
    assert [d["day_no"] for d in v["days"]] == [0, 1, 2, 3, 4, 5]


async def test_another_agency_cannot_read_or_change_a_version(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    other = await _setup(app_pool)
    v = await _create(app_pool, s)
    async with tenant_tx(app_pool, other["who"]["owner"], other["tenant"]) as ctx:
        with pytest.raises(NotFound):
            await builder.view(ctx, Vault(other["keys"], ctx), _pid(v))
        with pytest.raises(NotFound):
            await builder.update_settings(ctx, Vault(other["keys"], ctx), _pid(v), v["edit_seq"], {})
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.option_block") == 0
