"""AA-950 (card CRM-21b) end to end on PostgreSQL with the RDS role shape: the client's proposal page of a builder
version through ``client_link_tx()``, with real field encryption and an in-memory Booking (``tests/aa_fakes.py``).

- C-03: the choice with the deltas of D1 and the groups of R26; no net, margin or per-line price; "viewed" (R4) with
  one bell item for the first view; Keep / Swap re-prices; "Choose later" for agency items only (Fix B9).
- Fix A3: one hold set per version (a same-choice hold is reused, a changed AA pick re-holds after releasing the old
  one), a bell per re-hold; **card R**: a 4th re-check in a day is refused without calling Booking ("Ask your agent").
- **Card R**: accept after the hold expired -> 409 ``hold_expired`` with the re-quote offer; a re-check then lets the
  client accept, and the schedule follows the accepted picks.
- **Card E**: an option sells out mid-session -> the block falls back to its default and the total is recomputed; the
  same link works from a second device until it is re-issued (Fix B11).
- A new version on the same link shows one bundled notice; "Ask for a change" bundles the choice diff (R19); the nudge
  after 2 days (R4); Booking down; another agency sees nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Invalid, NotFound
from app.proposals import aa_options, agreements, builder, events, service
from app.proposals import public as page
from app.settings import Settings
from app.tenancy import system_tx, tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.test_builder_api import _as, _block, _create, _option, _pid, _publishable, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PLAIN = Settings(environment="test", db_ssl="disable")


async def _raw(pool: asyncpg.Pool, s: dict[str, Any], sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await conn.fetch(sql, *args)


async def _aa(pool: asyncpg.Pool, s: dict[str, Any], fake: FakeBooking, v: dict[str, Any], block: dict[str, Any],
              sell: str) -> dict[str, Any]:  # fmt: skip
    body = {"tour_source_id": "T-950", "component_id": "tp:1", "component_kind": "trip_plan",
            "sell_amount": Decimal(sell)}  # fmt: skip
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        plan = await builder.aa_plan(ctx, _pid(v), v["edit_seq"], uuid.UUID(block["option_block_id"]), body)
    quote = await aa_options.quote(booking_client(fake), s["tenant"], plan)
    return await _as(pool, s, "agent", builder.add_aa_option, plan, quote, body)


async def _sent(pool: asyncpg.Pool, *, riverside_requires_upgrade: bool = False,
                second_aa: bool = False) -> dict[str, Any]:  # fmt: skip
    """A sent builder version: four lodging nights (day 1: Lodge 1 or Riverside +40), a fixed park fee paid on
    arrival, an AA day tour on day 2 (500, or the 650 upgrade) and an agency dinner on day 4 that may wait. Booking
    keeps its own rules for one version (``strict_version_set``). ``riverside_requires_upgrade``: the agency's rule
    "Riverside 1 requires the 650 upgrade". ``second_aa``: a second AA block, "Rafting" on day 3 (200, or 230)."""
    s = await _setup(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ('T-950', 'Kathmandu valley day tour', 'NP', 1, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'"
        )
    fake = FakeBooking(b2b_price="300.00", availability="available", strict_version_set=True)
    v = await _publishable(pool, s)
    day2 = next(d for d in v["days"] if d["day_no"] == 2)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day2["proposal_day_id"]),
                  {"title": "Day tour", "type": "activity"})  # fmt: skip
    v = await _aa(pool, s, fake, v, _block(v, 2, 1), "500")
    v = await _aa(pool, s, fake, v, _block(v, 2, 1), "650")
    day4 = next(d for d in v["days"] if d["day_no"] == 4)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day4["proposal_day_id"]),
                  {"title": "Dinner", "type": "meal"})  # fmt: skip
    v = await _option(pool, s, v, _block(v, 4, 1), title="Cooking class dinner", sell_amount=Decimal("60"),
                      choose_later_allowed=True)  # fmt: skip
    if second_aa:
        day3 = next(d for d in v["days"] if d["day_no"] == 3)
        v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"],
                      uuid.UUID(day3["proposal_day_id"]), {"title": "Rafting", "type": "activity"})  # fmt: skip
        v = await _aa(pool, s, fake, v, _block(v, 3, 2), "200")
        v = await _aa(pool, s, fake, v, _block(v, 3, 2), "230")
    if riverside_requires_upgrade:
        riverside = next(o for o in _block(v, 1)["options"] if o["title"] == "Riverside 1")
        v = await _as(pool, s, "agent", builder.add_rule, _pid(v), v["edit_seq"],
                      {"kind": "requires", "option_id": uuid.UUID(riverside["option_id"]),
                       "target_option_id": uuid.UUID(_block(v, 2, 1)["options"][1]["option_id"])})  # fmt: skip
    v = await _as(pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
    assert sent["status"] == "sent" and sent["link_path"].startswith("/c/p/") and not sent["link_reused"]
    s.update(fake=fake, v=v, sent=sent, token=sent["link_path"].rsplit("/", 1)[1])
    return s


def _opt(p: dict[str, Any], title: str) -> dict[str, Any]:
    return next(o for d in p["days"] for b in d["blocks"] for o in b["options"] if o["title"] == title)


def _blk(p: dict[str, Any], title: str) -> dict[str, Any]:
    return next(b for d in p["days"] for b in d["blocks"] if any(o["title"] == title for o in b["options"]))


async def _pick(pool: asyncpg.Pool, s: dict[str, Any], title: str, **kw: Any) -> dict[str, Any]:
    p = await page.read(pool, s["keys"], s["token"])
    body = {"block_id": _blk(p, title)["option_block_id"], "option_id": _opt(p, title)["option_id"], **kw}
    return await page.pick(pool, s["keys"], PLAIN, s["token"], body, origin=None)


async def _hold(pool: asyncpg.Pool, s: dict[str, Any], fake: FakeBooking | None = None) -> tuple[int, dict]:
    return await page.hold(pool, s["keys"], PLAIN, booking_client(fake or s["fake"]), s["token"], origin=None)


async def _accept(pool: asyncpg.Pool, s: dict[str, Any], p: dict[str, Any], **kw: Any) -> dict[str, Any]:
    body = {"full_name": "Grace Lee", "accept_terms": True, "price_hash": p["price_hash"], "terms_doc_id": None,
            "selection_hash": p["selection_hash"], "rooms_confirmed": True, **kw}  # fmt: skip
    return await page.accept(pool, s["keys"], PLAIN, s["token"], body, origin=None)


async def _bell(pool: asyncpg.Pool, s: dict[str, Any], role: str = "agent") -> dict[str, Any]:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await events.bell(ctx)


def _bell_line(b: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((i for i in b["items"] if i["name"] == name), None)


async def test_c03_shows_the_choice_with_deltas_groups_and_viewed(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    p = await page.read(app_pool, s["keys"], s["token"])
    assert p["kind"] == "builder" and p["status"] == "sent" and p["version"] == 1
    # The defaults: 4 lodges 1000 + the AA day tour 500 + the dinner 60; the park fee is paid on arrival.
    assert p["totals"]["total"] == "1560.00" and p["totals"]["pay_on_arrival_total"] == "45.00"
    assert p["totals"]["deposit"] == "468.00" and p["total_amount"] == "1560.00"
    # D1: only the sell difference to the default per option; never a net, margin or per-line price (§2.9 rule 3).
    assert _opt(p, "Riverside 1")["delta"] == "40.00" and _opt(p, "Lodge 1")["delta"] == "0.00"
    assert "net" not in json.dumps(p) and "margin" not in json.dumps(p) and "sell_amount" not in json.dumps(p)
    # R26: grouped by confirmation class.
    classes = {g["confirm_class"]: [i["title"] for i in g["items"]] for g in p["groups"]}
    assert classes["on_payment"] == ["Kathmandu valley day tour"]
    assert "Cooking class dinner" in classes["agent"] and classes["arrival"] == ["Chitwan park fee"]
    assert p["hold"]["status"] == "missing" and p["hold"]["rechecks_left"] == 3 and p["notice"] is None
    # R4: viewed; the first view rings the trip owner's bell once; a re-read within the hour adds no event.
    await page.read(app_pool, s["keys"], s["token"])
    rows = await _raw(app_pool, s, "SELECT view_count, first_viewed_at FROM crm.proposal WHERE proposal_id = $1",
                      _pid(s["v"]))  # fmt: skip
    assert rows[0]["view_count"] == 2 and rows[0]["first_viewed_at"] is not None
    viewed = await _raw(app_pool, s, "SELECT count(*) AS n FROM crm.metric_event WHERE name = 'proposal.viewed'")
    assert viewed[0]["n"] == 1
    line = _bell_line(await _bell(app_pool, s), "proposal.viewed")
    assert line is not None and line["count"] == 1 and line["trip_title"] == "Nepal family"
    # The bell is the trip owner's: another member has nothing.
    assert (await _bell(app_pool, s, "owner"))["items"] == []


async def test_keep_swap_and_choose_later_for_agency_items_only(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    p = await _pick(app_pool, s, "Riverside 1")
    assert p["totals"]["total"] == "1600.00" and _opt(p, "Riverside 1")["chosen"]
    upgrade = _blk(p, "Kathmandu valley day tour")["options"][1]  # both AA options carry the tour's title
    p = await page.pick(app_pool, s["keys"], PLAIN, s["token"],
                        {"block_id": _blk(p, "Kathmandu valley day tour")["option_block_id"],
                         "option_id": upgrade["option_id"]}, origin=None)  # fmt: skip
    assert p["totals"]["total"] == "1750.00"
    # Fix B9: the dinner (an agency item) may wait; the AA tour may not.
    dinner = _blk(p, "Cooking class dinner")
    assert dinner["choose_later_allowed"] and not _blk(p, "Kathmandu valley day tour")["choose_later_allowed"]
    p = await page.pick(app_pool, s["keys"], PLAIN, s["token"], {"block_id": dinner["option_block_id"],
                        "choose_later": True}, origin=None)  # fmt: skip
    assert _blk(p, "Cooking class dinner")["choose_later"]
    with pytest.raises(Invalid) as err:
        await page.pick(app_pool, s["keys"], PLAIN, s["token"],
                        {"block_id": _blk(p, "Kathmandu valley day tour")["option_block_id"], "choose_later": True},
                        origin=None)  # fmt: skip
    assert err.value.code == "choose_later_not_allowed"
    # An option of another block is refused.
    with pytest.raises(Invalid) as err:
        await page.pick(app_pool, s["keys"], PLAIN, s["token"],
                        {"block_id": _blk(p, "Chitwan park fee")["option_block_id"],
                         "option_id": _opt(p, "Lodge 1")["option_id"]}, origin=None)  # fmt: skip
    assert err.value.code == "option_not_in_block"
    # The bell bundles the picks: one line with the count.
    line = _bell_line(await _bell(app_pool, s), "proposal.option_chosen")
    assert line is not None and line["count"] >= 3


async def test_fix_a3_one_hold_set_per_version_and_a_bell_per_rehold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    before = dict(fake.calls)
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["outcome"] == "held" and out["page"]["hold"]["status"] == "active"
    assert fake.calls["POST holds"] == before.get("POST holds", 0) + 1
    # The same choice again: the hold covers it, Booking is not asked.
    status, out = await _hold(app_pool, s)
    assert status == 200 and fake.calls["POST holds"] == before.get("POST holds", 0) + 1
    # An agency pick changes nothing to hold.
    await _pick(app_pool, s, "Riverside 1")
    p = await page.read(app_pool, s["keys"], s["token"])
    assert p["hold"]["status"] == "active"
    # An AA pick needs a new hold: the old one is released first (one active set per version), a re-check is used.
    tour = _blk(p, "Kathmandu valley day tour")
    p = await page.pick(app_pool, s["keys"], PLAIN, s["token"], {"block_id": tour["option_block_id"],
                        "option_id": tour["options"][1]["option_id"]}, origin=None)  # fmt: skip
    assert p["hold"]["status"] == "stale"
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active" and out["page"]["hold"]["rechecks_left"] == 2
    assert fake.calls["DELETE holds"] == 1 and fake.calls["POST holds"] == before.get("POST holds", 0) + 2
    rows = await _raw(app_pool, s, "SELECT hold_count, hold_set_ref, recheck_count_day FROM crm.proposal "
                      "WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    assert rows[0]["hold_count"] == 2 and rows[0]["hold_set_ref"] == f"crm-proposal:{s['v']['proposal_id']}"
    assert rows[0]["recheck_count_day"] == 1
    assert _bell_line(await _bell(app_pool, s), "proposal.rehold")["count"] == 1  # type: ignore[index]
    # No personal data went to Booking: the agency id header only.
    assert set(fake.tenants) == {str(s["tenant"])}


async def test_card_r_a_4th_recheck_in_a_day_is_refused_without_a_hold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    fake.hold_expires_in = dt.timedelta(seconds=-60)  # every hold has run out by the time the client looks again
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "expired" and out["page"]["hold"]["rechecks_left"] == 3
    for left in (2, 1, 0):
        status, out = await _hold(app_pool, s)
        assert status == 200 and out["page"]["hold"]["rechecks_left"] == left
    holds_before = fake.calls["POST holds"]
    status, out = await _hold(app_pool, s)
    assert status == 429 and out["detail"] == "recheck_limit" and "Ask your agent" in out["message"]
    assert fake.calls["POST holds"] == holds_before  # no hold, Booking not asked
    assert _bell_line(await _bell(app_pool, s), "proposal.recheck_refused") is not None


async def test_card_r_accept_after_the_hold_expired_offers_a_requote(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    p = await page.read(app_pool, s["keys"], s["token"])
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, p)
    assert err.value.code == "hold_required"
    with pytest.raises(Invalid):
        await _accept(app_pool, s, p, rooms_confirmed=False)  # Fix B10: the client confirms the rooms
    s["fake"].hold_expires_in = dt.timedelta(seconds=-1)
    _, out = await _hold(app_pool, s)
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, out["page"])
    assert err.value.code == "hold_expired"
    assert err.value.extra["requote"] == {"available": True, "rechecks_left": 3}
    rows = await _raw(app_pool, s, "SELECT status FROM crm.proposal WHERE proposal_id = $1", _pid(s["v"]))
    assert rows[0]["status"] == "sent"  # nothing accepted
    # C-12 "Check availability again" -> a new hold, then the client accepts; a stale choice hash is refused first.
    s["fake"].hold_expires_in = None
    await _pick(app_pool, s, "Riverside 1")
    _, out = await _hold(app_pool, s)
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, out["page"], selection_hash="0" * 64)
    assert err.value.code == "choice_changed" and err.value.extra["selection_hash"] == out["page"]["selection_hash"]
    done = await _accept(app_pool, s, out["page"])
    assert done["status"] == "accepted"
    rows = await _raw(app_pool, s, "SELECT accepted_total_amount, accepted_deposit_amount, accepted_hold_id, "
                      "hold_id FROM crm.proposal WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    assert rows[0]["accepted_total_amount"] == Decimal("1600.00") and rows[0]["accepted_deposit_amount"] == Decimal(
        "480.00")  # fmt: skip
    assert rows[0]["accepted_hold_id"] == rows[0]["hold_id"]
    sched = await _raw(app_pool, s, "SELECT kind, amount FROM crm.payment_schedule WHERE proposal_id = $1 "
                       "ORDER BY kind", _pid(s["v"]))  # fmt: skip
    assert [(r["kind"], r["amount"]) for r in sched] == [
        ("balance", Decimal("1120.00")),
        ("deposit", Decimal("480.00")),
    ]
    task = await _raw(app_pool, s, "SELECT due_anchor FROM crm.task WHERE trip_id = $1 AND kind = 'collect_deposit'",
                      s["trip"])  # fmt: skip
    assert task[0]["due_anchor"] == "hold_expiry"
    after = await page.read(app_pool, s["keys"], s["token"])
    assert after["status"] == "accepted" and after["total_amount"] == "1600.00"
    again = await _accept(app_pool, s, out["page"])
    assert again["status"] == "already_accepted"


async def test_card_e_an_option_sold_out_mid_session_falls_back_to_the_default(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    p = await page.read(app_pool, s["keys"], s["token"])
    tour = _blk(p, "Kathmandu valley day tour")
    upgrade = tour["options"][1]
    p = await page.pick(app_pool, s["keys"], PLAIN, s["token"], {"block_id": tour["option_block_id"],
                        "option_id": upgrade["option_id"]}, origin=None)  # fmt: skip
    assert p["totals"]["total"] == "1710.00"
    s["fake"].line_availability[upgrade["option_id"]] = "sold_out"
    status, out = await _hold(app_pool, s)
    assert status == 409 and out["detail"] == "option_sold_out" and out["ask_agent"] is False
    shown = out["page"]
    assert shown["totals"]["total"] == "1560.00"  # the default again, total recomputed
    options = _blk(shown, "Kathmandu valley day tour")["options"]
    assert options[0]["chosen"] and options[1]["blocked"] == {"reason": "sold_out"} and not options[1]["chosen"]
    assert _bell_line(await _bell(app_pool, s), "proposal.option_sold_out") is not None
    with pytest.raises(Conflict) as err:
        await page.pick(app_pool, s["keys"], PLAIN, s["token"], {"block_id": tour["option_block_id"],
                        "option_id": upgrade["option_id"]}, origin=None)  # fmt: skip
    assert err.value.code == "option_sold_out"
    # The default holds fine.
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"


async def test_card_e_the_same_link_opens_on_a_second_device_until_reissued(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    first = await page.read(app_pool, s["keys"], s["token"])  # phone
    second = await page.read(app_pool, s["keys"], s["token"])  # laptop / spouse: nothing binds the link to a device
    assert first["proposal_id"] == second["proposal_id"] and second["status"] == "sent"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        fresh = await service.reissue_link(ctx, _pid(s["v"]))
    with pytest.raises(NotFound):
        await page.read(app_pool, s["keys"], s["token"])
    s["token"] = fresh["link_path"].rsplit("/", 1)[1]
    assert (await page.read(app_pool, s["keys"], s["token"]))["version"] == 1
    # The assistant may not re-issue (PROPOSAL_PUBLISH is owner / agent; the route checks it).


async def test_a_new_version_takes_over_the_link_with_one_bundled_notice(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    await _pick(app_pool, s, "Riverside 1")
    v2 = await _create(app_pool, s, from_proposal_id=_pid(s["v"]))
    v2 = await _option(app_pool, s, v2, _block(v2, 3), title="Jungle lodge", sell_amount=Decimal("270"))
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v2), v2["edit_seq"], acknowledge_selection_seq=1)
    assert out["publish"]["picks_carried"] == 1
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v2))
    assert sent["link_reused"] is True and sent["link_path"] is None and sent["superseded"] == s["v"]["proposal_id"]
    p = await page.read(app_pool, s["keys"], s["token"])  # the client's old link (and cookie) shows v2
    assert p["version"] == 2 and _opt(p, "Riverside 1")["chosen"]
    notice = p["notice"]
    assert notice["from_version"] == 1 and notice["picks_carried"] == 1
    assert {"kind": "added", "title": "Jungle lodge"} in notice["changes"]
    # The client's totals: v1 with their Riverside pick (not v1's published 1560), and this page's total.
    assert notice["total_from"] == "1600.00" and notice["total_to"] == p["totals"]["total"]


async def test_ask_for_a_change_bundles_the_choice_diff(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    await _pick(app_pool, s, "Riverside 1")
    with pytest.raises(Invalid):
        await page.change_request(app_pool, s["keys"], PLAIN, s["token"], {"reason": "please call me"}, origin=None)
    done = await page.change_request(app_pool, s["keys"], PLAIN, s["token"], {"reason": "rooms"}, origin=None)
    assert done == {"status": "sent", "reason": "rooms"}
    line = _bell_line(await _bell(app_pool, s), "proposal.change_requested")
    assert line is not None and line["detail"]["reason"] == "rooms"
    assert line["detail"]["choices"][0]["chosen"] == "Riverside 1" and line["detail"]["choices"][0]["sell_delta"] == (
        "40.00")  # fmt: skip
    task = await _raw(app_pool, s, "SELECT count(*) AS n FROM crm.task WHERE trip_id = $1 "
                      "AND kind = 'proposal_change_request'", s["trip"])  # fmt: skip
    assert task[0]["n"] == 1
    # The agent's view (A-03): R19 choices, viewed, events; the assistant reads it too (no net there either).
    for role in ("agent", "assistant"):
        async with tenant_tx(app_pool, s["who"][role], s["tenant"]) as ctx:
            act = await service.client_activity(ctx, _pid(s["v"]))
        assert act["choices"][0]["block"] == "Lodging" and act["view_count"] >= 1
        assert act["totals"] == {"published": "1560.00", "current": "1600.00", "currency": "USD"}
        assert "net" not in json.dumps(act)
    # Marking the bell seen empties it.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert (await events.mark_seen(ctx, None))["seen"] >= 2
    assert (await _bell(app_pool, s))["items"] == []


async def test_r4_the_nudge_after_two_days_viewed_without_a_choice(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    await page.read(app_pool, s["keys"], s["token"])
    await _raw(app_pool, s, "UPDATE crm.proposal SET first_viewed_at = now() - interval '3 days', "
               "last_viewed_at = now() - interval '3 days' WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert await events.nudge_due(ctx) == 1
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert await events.nudge_due(ctx) == 0  # once per version
    task = await _raw(app_pool, s, "SELECT count(*) AS n FROM crm.task WHERE trip_id = $1 "
                      "AND kind = 'proposal_follow_up'", s["trip"])  # fmt: skip
    assert task[0]["n"] == 1 and _bell_line(await _bell(app_pool, s), "proposal.nudge_due") is not None


async def test_booking_down_disables_the_hold_and_tells_the_agent_once(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    down = FakeBooking(down=True)
    for _ in range(2):
        status, out = await _hold(app_pool, s, down)
        assert status == 503 and out["detail"] == "booking_unavailable" and out["page"]["hold"]["status"] == "missing"
    assert _bell_line(await _bell(app_pool, s), "proposal.booking_unavailable")["count"] == 1  # type: ignore[index]


async def test_another_agency_sees_no_activity_and_no_bell(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    await page.read(app_pool, s["keys"], s["token"])
    other = await _setup(app_pool)
    async with tenant_tx(app_pool, other["who"]["agent"], other["tenant"]) as ctx:
        with pytest.raises(NotFound):
            await service.client_activity(ctx, _pid(s["v"]))
        assert (await events.bell(ctx))["items"] == []
        with pytest.raises(NotFound):
            await service.reissue_link(ctx, _pid(s["v"]))


# ── Fix round 2: the hold row never claims a hold Booking let go; a lost answer is recovered; the rules hold ──


async def _swap_to_upgrade(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    p = await page.read(pool, s["keys"], s["token"])
    tour = _blk(p, "Kathmandu valley day tour")
    return await page.pick(pool, s["keys"], PLAIN, s["token"], {"block_id": tour["option_block_id"],
                           "option_id": tour["options"][1]["option_id"]}, origin=None)  # fmt: skip


async def _hold_row(pool: asyncpg.Pool, s: dict[str, Any]) -> Any:
    rows = await _raw(pool, s, "SELECT hold_id, hold_count, hold_attempt, recheck_count_day, hold_pending_key, "
                      "hold_pending_quote_id FROM crm.proposal WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    return rows[0]


async def test_card_e_a_sold_out_swap_keeps_the_hold_of_the_default(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    status, out = await _hold(app_pool, s)  # the default AA option is held (h1)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    p = await _swap_to_upgrade(app_pool, s)
    fake.line_availability[_blk(p, "Kathmandu valley day tour")["options"][1]["option_id"]] = "sold_out"
    status, out = await _hold(app_pool, s)
    assert status == 409 and out["detail"] == "option_sold_out"
    # The quote said sold out before anything was released: h1 is still held at Booking and covers the fallback.
    assert fake.calls["DELETE holds"] == 0 and fake.holds["h1"]["status"] == "active"
    assert out["page"]["hold"]["status"] == "active" and out["page"]["totals"]["total"] == "1560.00"
    done = await _accept(app_pool, s, out["page"])
    assert done["status"] == "accepted"
    rows = await _raw(app_pool, s, "SELECT accepted_hold_id FROM crm.proposal WHERE proposal_id = $1", _pid(s["v"]))
    assert rows[0]["accepted_hold_id"] == "h1" and fake.holds["h1"]["status"] == "active"


async def test_a_refused_rehold_after_the_release_leaves_no_phantom_hold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default
    await _swap_to_upgrade(app_pool, s)
    fake.hold_refusal = "sold_out"  # the quote is clean, Booking refuses the hold itself (after h1 was released)
    status, out = await _hold(app_pool, s)
    assert status == 409 and out["detail"] == "hold_unavailable"
    assert fake.holds["h1"]["status"] == "released"
    assert out["page"]["hold"]["status"] == "missing"
    row = await _hold_row(app_pool, s)
    assert row["hold_id"] is None and row["hold_pending_key"] is None  # Booking answered: nothing in flight
    # The client swaps back by hand: the default's old hold is gone at Booking, so it is not shown as held.
    p = await page.read(app_pool, s["keys"], s["token"])
    tour = _blk(p, "Kathmandu valley day tour")
    p = await page.pick(app_pool, s["keys"], PLAIN, s["token"], {"block_id": tour["option_block_id"],
                        "option_id": tour["options"][0]["option_id"]}, origin=None)  # fmt: skip
    assert p["hold"]["status"] == "missing"
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, p)
    assert err.value.code == "hold_required"


async def test_a_booking_outage_after_the_release_clears_the_hold_too(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)
    await _swap_to_upgrade(app_pool, s)
    fake.lose_next_hold_answer = True  # h1 released, the new hold made, its answer lost
    status, out = await _hold(app_pool, s)
    assert status == 503 and out["page"]["hold"]["status"] == "missing"
    row = await _hold_row(app_pool, s)
    assert row["hold_id"] is None and row["hold_pending_quote_id"] is not None  # kept for the replay


async def test_a_lost_hold_answer_is_replayed_and_recovered(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    fake.lose_next_hold_answer = True  # Booking makes h1, the CRM never hears back
    status, out = await _hold(app_pool, s)
    assert status == 503 and out["page"]["hold"]["status"] == "missing" and fake.holds["h1"]["status"] == "active"
    row = await _hold_row(app_pool, s)
    assert row["hold_pending_key"] == f"crm-proposal:{s['v']['proposal_id']}:v1:a1"
    quotes = fake.quotes
    status, out = await _hold(app_pool, s)  # the same key and quote again: Booking answers with h1
    assert status == 200 and out["outcome"] == "held" and out["page"]["hold"]["status"] == "active"
    assert fake.quotes == quotes and len(fake.holds) == 1
    row = await _hold_row(app_pool, s)
    assert row["hold_id"] == "h1" and row["hold_count"] == 1 and row["recheck_count_day"] == 0
    assert row["hold_pending_key"] is None
    done = await _accept(app_pool, s, out["page"])
    assert done["status"] == "accepted"


async def test_a_lost_answer_for_an_earlier_choice_is_recovered_then_replaced(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    fake.lose_next_hold_answer = True
    await _hold(app_pool, s)  # h1 for the default, answer lost
    await _swap_to_upgrade(app_pool, s)
    # One press: the replay recovers h1 (it blocks the version at Booking), then the upgrade is held in its place.
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    assert fake.holds["h1"]["status"] == "released" and fake.holds["h2"]["status"] == "active"
    row = await _hold_row(app_pool, s)
    assert row["hold_id"] == "h2" and row["hold_count"] == 2 and row["hold_attempt"] == 2
    assert row["recheck_count_day"] == 1 and row["hold_pending_key"] is None


async def test_a_second_screen_waits_while_the_first_is_quoting(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    key = f"crm-proposal:{s['v']['proposal_id']}:v1:a1"
    await _raw(app_pool, s, "UPDATE crm.proposal SET hold_attempt = 1, hold_pending_key = $2, hold_pending_at = now() "
               "WHERE proposal_id = $1", _pid(s["v"]), key)  # fmt: skip
    quotes = fake.quotes
    status, out = await _hold(app_pool, s)
    assert status == 409 and out["detail"] == "hold_in_progress" and fake.quotes == quotes
    # A request that never reached its quote within 2 minutes died: a new one (a fresh key) replaces it.
    await _raw(app_pool, s, "UPDATE crm.proposal SET hold_pending_at = now() - interval '5 minutes' "
               "WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    assert fake.hold_keys.get(f"crm-proposal:{s['v']['proposal_id']}:v1:a2") == "h1"


async def test_card_e_a_fallback_that_breaks_a_rule_is_neither_held_nor_accepted(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool, riverside_requires_upgrade=True)
    fake = s["fake"]
    p = await _swap_to_upgrade(app_pool, s)
    upgrade = _blk(p, "Kathmandu valley day tour")["options"][1]["option_id"]
    p = await _pick(app_pool, s, "Riverside 1")  # allowed: the upgrade it requires is chosen
    fake.line_availability[upgrade] = "sold_out"
    status, out = await _hold(app_pool, s)
    assert status == 409 and out["detail"] == "option_sold_out"
    # The tour fell back to the default; Riverside 1 still needs the upgrade: no hold, no accept.
    holds_before = fake.calls["POST holds"]
    status, out = await _hold(app_pool, s)
    assert status == 409 and out["detail"] == "option_conflict" and fake.calls["POST holds"] == holds_before
    assert out["violations"][0]["option"] == "Riverside 1"
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, out["page"])
    assert err.value.code == "option_conflict"
    # Keeping Lodge 1 instead fixes it.
    p = await _pick(app_pool, s, "Lodge 1")
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"


# ── Fix round 3: an unanswered release, accept during another screen's hold, the version's own seats ──


def _titled(p: dict[str, Any], block_title: str) -> dict[str, Any]:
    return next(b for d in p["days"] for b in d["blocks"] if b["title"] == block_title)


async def _choose(pool: asyncpg.Pool, s: dict[str, Any], block_title: str, index: int) -> dict[str, Any]:
    p = await page.read(pool, s["keys"], s["token"])
    block = _titled(p, block_title)
    return await page.pick(pool, s["keys"], PLAIN, s["token"], {"block_id": block["option_block_id"],
                           "option_id": block["options"][index]["option_id"]}, origin=None)  # fmt: skip


async def _release_row(pool: asyncpg.Pool, s: dict[str, Any]) -> Any:
    rows = await _raw(pool, s, "SELECT hold_id, hold_release_id, hold_pending_key, accepted_hold_id, status "
                      "FROM crm.proposal WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    return rows[0]


async def test_a_release_without_an_answer_never_leaves_a_phantom_hold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default
    await _swap_to_upgrade(app_pool, s)
    fake.lose_next_release_answer = True  # Booking released h1; the CRM never heard back
    status, out = await _hold(app_pool, s)
    assert status == 503 and out["detail"] == "booking_unavailable" and out["page"]["hold"]["status"] == "missing"
    assert fake.holds["h1"]["status"] == "released" and fake.calls["POST holds"] == 1
    row = await _release_row(app_pool, s)
    assert row["hold_id"] is None and row["hold_release_id"] == "h1" and row["hold_pending_key"] is None
    # The client swaps back to the old picks: h1 is not shown as held, and accept does not seal it.
    p = await _choose(app_pool, s, "Day tour", 0)
    assert p["hold"]["status"] == "missing"
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, p)
    assert err.value.code == "hold_required"
    # The next check releases h1 again (a no-op at Booking) and holds the choice.
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    assert fake.calls["DELETE holds"] == 2 and fake.holds["h2"]["status"] == "active"
    row = await _release_row(app_pool, s)
    assert row["hold_id"] == "h2" and row["hold_release_id"] is None
    assert (await _accept(app_pool, s, out["page"]))["status"] == "accepted"
    assert (await _release_row(app_pool, s))["accepted_hold_id"] == "h2"


async def test_a_release_that_failed_at_booking_is_sent_again_before_the_next_hold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)
    await _swap_to_upgrade(app_pool, s)
    fake.fail_next_release = True  # a 5xx before Booking released anything: h1 is still Booking's set for v1
    status, out = await _hold(app_pool, s)
    assert status == 503 and out["page"]["hold"]["status"] == "missing" and fake.holds["h1"]["status"] == "active"
    # Without the second release Booking would refuse the new quote (one set per version): it is released first.
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    assert fake.holds["h1"]["status"] == "released" and fake.holds["h2"]["status"] == "active"
    row = await _release_row(app_pool, s)
    assert row["hold_id"] == "h2" and row["hold_release_id"] is None


class _Died(Exception):
    """The process stopped mid-request (a restart, a dropped connection): nothing after it runs."""


async def test_a_replay_while_a_hold_waits_for_its_release_stores_the_new_hold(app_pool: asyncpg.Pool) -> None:
    # Fix round 4: a parked hold (its release got no answer), then a request that dies after Booking made the new
    # hold: the replay must store it and forget the parked one (Booking keeps one active set per version).
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default
    await _swap_to_upgrade(app_pool, s)
    fake.lose_next_release_answer = True  # Booking released h1; the CRM never heard back
    status, _ = await _hold(app_pool, s)
    assert status == 503
    row = await _release_row(app_pool, s)
    assert row["hold_id"] is None and row["hold_release_id"] == "h1" and row["hold_pending_key"] is None

    async def died() -> None:
        raise _Died  # Booking released h1 (again) and made h2; the CRM never reaches finish()

    fake.on_hold = died
    with pytest.raises(_Died):
        await _hold(app_pool, s)
    assert fake.holds["h1"]["status"] == "released" and fake.holds["h2"]["status"] == "active"
    row = await _release_row(app_pool, s)
    assert row["hold_id"] is None and row["hold_release_id"] == "h1" and row["hold_pending_key"] is not None
    # The next check replays the request in flight: Booking answers h2, which goes on the row; h1 is forgotten.
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    assert fake.calls["POST holds"] == 3 and fake.calls["DELETE holds"] == 2 and len(fake.holds) == 2
    row = await _release_row(app_pool, s)
    assert row["hold_id"] == "h2" and row["hold_release_id"] is None and row["hold_pending_key"] is None
    assert (await _accept(app_pool, s, out["page"]))["status"] == "accepted"
    assert (await _release_row(app_pool, s))["accepted_hold_id"] == "h2"


async def test_a_lost_request_is_replayed_even_when_the_rows_hold_covers_the_choice(app_pool: asyncpg.Pool) -> None:
    # Fix round 4 (same class): a request dies after Booking released h1 and made h2 for another pick; the client goes
    # back to the pick h1 covers. Accept refuses (hold_required: a request lost its answer), so "check availability"
    # must replay that request, not answer "held" with h1 (Booking no longer has it) and leave the request forever.
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default

    async def died() -> None:
        raise _Died

    await _swap_to_upgrade(app_pool, s)
    fake.on_hold = died
    with pytest.raises(_Died):
        await _hold(app_pool, s)
    assert fake.holds["h1"]["status"] == "released" and fake.holds["h2"]["status"] == "active"
    await _raw(app_pool, s, "UPDATE crm.proposal SET hold_pending_at = now() - interval '5 minutes' "
               "WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    p = await _choose(app_pool, s, "Day tour", 0)  # back to the default, which the row's h1 covers
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, p)
    assert err.value.code == "hold_required"
    # The check replays the lost request (h2, the upgrade), then holds the current choice (h2 released, h3).
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    assert fake.holds["h2"]["status"] == "released" and fake.holds["h3"]["status"] == "active"
    row = await _release_row(app_pool, s)
    assert row["hold_id"] == "h3" and row["hold_release_id"] is None and row["hold_pending_key"] is None
    assert (await _accept(app_pool, s, out["page"]))["status"] == "accepted"
    assert (await _release_row(app_pool, s))["accepted_hold_id"] == "h3"


@pytest.mark.parametrize("refusal", ["sold_out", "rehold_limit"])
async def test_a_refused_replay_never_answers_held_on_a_hold_the_lost_request_released(
    app_pool: asyncpg.Pool, refusal: str
) -> None:
    # Fix round 5 (same class as round 4, through a replay Booking refuses): a request released h1 and died before
    # Booking made a new hold; the client goes back to the pick h1 covers. The replay is refused (the upgrade sold out
    # once h1 freed its seats, or Booking's own re-hold limit): h1 may be gone at Booking, so it is parked, never
    # answered "held" and never sealed by accept.
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default
    await _swap_to_upgrade(app_pool, s)

    async def died() -> None:
        raise _Died  # Booking released h1; the CRM dies before POST /holds

    fake.on_release = died
    with pytest.raises(_Died):
        await _hold(app_pool, s)
    assert fake.holds["h1"]["status"] == "released" and len(fake.holds) == 1 and fake.calls["POST holds"] == 1
    row = await _release_row(app_pool, s)
    assert row["hold_id"] == "h1" and row["hold_release_id"] is None and row["hold_pending_key"] is not None
    await _raw(app_pool, s, "UPDATE crm.proposal SET hold_pending_at = now() - interval '5 minutes' "
               "WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
    await _choose(app_pool, s, "Day tour", 0)  # back to the default, which the row's h1 covers
    fake.hold_refusal = refusal
    status, out = await _hold(app_pool, s)
    assert not (status == 200 and out.get("outcome") == "held")
    assert out["page"]["hold"]["status"] == "missing"
    row = await _release_row(app_pool, s)
    assert row["hold_id"] is None and row["hold_pending_key"] is None
    if refusal == "rehold_limit":
        # Booking's per-departure limit: nothing more this time; h1 waits to be released again.
        assert status == 429 and out["detail"] == "recheck_limit" and row["hold_release_id"] == "h1"
    else:
        # The replay is gone; the new request released h1 again (a no-op) and Booking refused that hold too.
        assert status == 409 and out["detail"] == "hold_unavailable" and row["hold_release_id"] is None
        assert fake.calls["DELETE holds"] == 2
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, out["page"])
    assert err.value.code == "hold_required"
    assert (await _release_row(app_pool, s))["accepted_hold_id"] is None
    # Booking holds again: the next check places a new hold for the default, which accept seals.
    fake.hold_refusal = None
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active" and fake.holds["h2"]["status"] == "active"
    row = await _release_row(app_pool, s)
    assert row["hold_id"] == "h2" and row["hold_release_id"] is None and row["hold_pending_key"] is None
    assert (await _accept(app_pool, s, out["page"]))["status"] == "accepted"
    assert (await _release_row(app_pool, s))["accepted_hold_id"] == "h2"


async def test_accept_waits_while_another_screen_places_the_hold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default
    await _swap_to_upgrade(app_pool, s)  # screen 1 picks the upgrade and presses Continue
    seen: dict[str, Any] = {}

    async def second_screen() -> None:
        # Booking has released h1 and made h2; screen 2 (same link) picks the default back: the row still names h1.
        p = await _choose(app_pool, s, "Day tour", 0)
        seen["shown"] = p["hold"]["status"]
        with pytest.raises(Conflict) as err:
            await _accept(app_pool, s, p)
        seen["accept"] = err.value.code

    fake.on_hold = second_screen
    status, out = await _hold(app_pool, s)
    assert seen == {"shown": "active", "accept": "hold_in_progress"}
    assert status == 200 and out["page"]["hold"]["status"] == "stale"  # h2 holds the upgrade; the choice is back
    assert fake.holds["h1"]["status"] == "released"
    row = await _release_row(app_pool, s)
    assert row["status"] == "sent" and row["hold_id"] == "h2" and row["accepted_hold_id"] is None
    with pytest.raises(Conflict) as err:
        await _accept(app_pool, s, out["page"])
    assert err.value.code == "hold_required"


async def test_a_hold_request_that_lost_the_race_with_accept_releases_nothing(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool)
    fake = s["fake"]
    await _hold(app_pool, s)  # h1 holds the default
    await _swap_to_upgrade(app_pool, s)
    seen: dict[str, Any] = {}

    async def stalled_then_accepted() -> None:
        # Screen 1's request stalled past PENDING_STALE before its quote came back (it reads as dead); screen 2 picks
        # the default back and accepts on h1.
        await _raw(app_pool, s, "UPDATE crm.proposal SET hold_pending_at = now() - interval '5 minutes' "
                   "WHERE proposal_id = $1", _pid(s["v"]))  # fmt: skip
        p = await _choose(app_pool, s, "Day tour", 0)
        seen["accept"] = (await _accept(app_pool, s, p))["status"]

    fake.on_quote = stalled_then_accepted
    with pytest.raises(Conflict) as err:
        await _hold(app_pool, s)
    assert err.value.code == "already_accepted" and seen == {"accept": "accepted"}
    # Screen 1 never wrote its quote down, so it released nothing and held nothing: h1 is the accepted hold.
    assert fake.calls["DELETE holds"] == 0 and fake.calls["POST holds"] == 1 and fake.holds["h1"]["status"] == "active"
    row = await _release_row(app_pool, s)
    assert row["status"] == "accepted" and row["accepted_hold_id"] == "h1"


async def test_a_rehold_quote_does_not_count_the_seats_of_the_versions_own_hold(app_pool: asyncpg.Pool) -> None:
    s = await _sent(app_pool, second_aa=True)
    fake = s["fake"]
    status, out = await _hold(app_pool, s)  # h1 holds both AA defaults (the day tour and rafting)
    assert status == 200 and out["page"]["hold"]["status"] == "active"
    tour = _titled(out["page"], "Day tour")["options"][0]["option_id"]
    seats = fake.quote_seats[fake.hold_meta["h1"][0]][tour]
    fake.seat_capacity[tour] = seats  # h1 holds the departure's last seats: the departure is full
    assert fake.held_seats(tour) == seats
    # The client swaps the other AA pick (rafting 200 -> 230): the re-hold quotes the day tour again with it.
    await _choose(app_pool, s, "Rafting", 1)
    status, out = await _hold(app_pool, s)
    assert status == 200 and out["page"]["hold"]["status"] == "active"  # not sold out: those seats are this client's
    assert fake.holds["h1"]["status"] == "released" and fake.holds["h2"]["status"] == "active"
    options = await _raw(app_pool, s, "SELECT availability FROM crm.option WHERE option_id = $1", uuid.UUID(tour))
    assert options[0]["availability"] != "sold_out"
    assert _titled(out["page"], "Day tour")["options"][0]["chosen"]
    # Anyone else (another proposal) still finds the departure full: h2 holds those seats now.
    other = {"proposal_ref": "crm-proposal:other", "proposal_version": 1}
    assert fake._quote_availability(other, {"line_ref": tour, "pax": {"adult": 1}}) == "sold_out"


async def test_a_builder_version_carries_the_published_terms_and_no_reaccept_waits(app_pool: asyncpg.Pool) -> None:
    """S261 live finding (AA-952 x AA-955): a builder version saved no terms, so the client accepted none and the
    deposit then waited for a re-accept. With booking terms published, a new version starts with them, a version
    whose terms were cleared is pinned to them at publish, the client accepts them with the proposal and the deposit
    waits for nothing (design v3 F6 step 3, WF-7, R34)."""
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        doc = await agreements.create(ctx, kind="booking_terms", version_label="v2026-12", body_text="Our terms.")
        await agreements.publish(ctx, uuid.UUID(doc["doc_id"]))
    terms_id = doc["doc_id"]
    v = await _publishable(app_pool, s)
    assert v["terms_doc_id"] == terms_id  # the skeleton starts with the published terms
    v = await _as(app_pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"], {"terms_doc_id": None})
    assert v["terms_doc_id"] is None  # cleared by hand (the API path the live check used) ...
    v = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert v["terms_doc_id"] == terms_id  # ... and pinned again at publish
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
    s["token"] = sent["link_path"].rsplit("/", 1)[1]
    p = await page.read(app_pool, s["keys"], s["token"])
    assert p["terms"]["version_label"] == "v2026-12"
    with pytest.raises(Conflict) as err:  # the page shows the terms: accepting without them is refused
        await _accept(app_pool, s, p)
    assert err.value.code == "terms_changed"
    accepted = await _accept(app_pool, s, p, terms_doc_id=terms_id)
    assert accepted["status"] == "accepted" and accepted["terms_reaccept_required"] is False
    assert (await page.read(app_pool, s["keys"], s["token"]))["terms_reaccept"] is None
