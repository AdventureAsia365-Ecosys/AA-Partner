"""AA-1144: an agency sells AA's products and keeps only flights, visas, travel insurance and its own fee.

The proposal builder, the booking lines, the supplier directory and the service catalogue refuse a hotel, transfer,
guide, ticket, meal, activity or DMC item of the agency's own (422 with a plain reason), and "Ask AA for extra nights"
(B11) files a request AA ground operations read on the staff console.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.booking import extra_nights
from app.catalogue import service as catalogue
from app.errors import Conflict, Invalid
from app.ledger import lines, suppliers
from app.proposals import builder
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_builder_api import _as, _create, _pid, _publishable, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKFORCE = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTWORKF"


def _ops() -> Caller:
    return Caller("staff", "workforce", "crm-staff", WORKFORCE, str(uuid.uuid4()), ("crm-ops",))


async def _add(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], block: dict[str, Any], **body: Any):
    body.setdefault("title", "Lantern Air HAN-DAD")
    body.setdefault("sell_amount", Decimal("120"))
    return await _as(
        pool, s, "agent", builder.add_agency_option, _pid(v), v["edit_seq"], uuid.UUID(block["option_block_id"]), body
    )


async def _other_block(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], kind: str = "other") -> dict:
    day1 = next(d for d in v["days"] if d["day_no"] == 1)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day1["proposal_day_id"]),
                  {"title": f"{kind.title()} items", "type": kind})  # fmt: skip
    return v


async def test_the_builder_takes_agency_items_of_the_closed_list_on_an_other_block_only(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    # The skeleton's lodging block (and an activity or transport block) takes AA items only: a hotel night is AA's.
    for kind in ("activity", "transport", "meal", "guide"):
        v = await _other_block(app_pool, s, v, kind)
    for block in _block_list(v):
        with pytest.raises(Invalid) as err:
            await _add(app_pool, s, v, block, title="Riverside homestay", category="fee")
        assert err.value.code == "agency_item_not_allowed" and "Adventure Asia" in err.value.message
    v = await _other_block(app_pool, s, v)
    other = next(b for b in _block_list(v) if b["type"] == "other")
    # Outside the closed list (a transfer, the "other" loophole, nothing): refused with the reason.
    for category in ("transfer", "other", "hotel", None):
        with pytest.raises(Invalid) as err:
            await _add(app_pool, s, v, other, category=category)
        assert err.value.code == "invalid_category" and err.value.extra["allowed"] == [
            "flight", "visa", "insurance", "fee",
        ]  # fmt: skip
    for category in ("flight", "visa", "insurance", "fee"):
        v = await _add(app_pool, s, v, other, title=f"Agency {category}", category=category)
    other = next(b for b in _block_list(v) if b["type"] == "other")
    assert [o["category"] for o in other["options"]] == ["flight", "visa", "insurance", "fee"]
    # A block holding agency items keeps its type: no way round the rule by retyping it.
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.update_block, _pid(v), v["edit_seq"],
                  uuid.UUID(other["option_block_id"]), {"type": "lodging"})  # fmt: skip
    assert err.value.code == "agency_item_not_allowed"
    v = await _as(app_pool, s, "agent", builder.update_block, _pid(v), v["edit_seq"],
                  uuid.UUID(other["option_block_id"]), {"title": "Flights and visas"})  # fmt: skip


async def test_a_working_copy_carries_the_item_types(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)  # its agency items are fees on Other blocks (the shared helper)
    v1 = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    copy = await _create(app_pool, s, from_proposal_id=_pid(v1))
    agency = [o for b in _block_list(copy) for o in b["options"] if o["source"] == "agency"]
    assert agency and {o["category"] for o in agency} == {"fee"}


def _block_list(v: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for d in v["days"] for b in d["blocks"]]


async def test_lines_suppliers_and_services_keep_to_the_closed_list(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        for category in ("accommodation", "transport", "activity", "guide", "restaurant", "dmc"):
            with pytest.raises(Invalid) as err:
                await suppliers.create_supplier(ctx, vault, {"name": "Pine Cove Lodge", "category": category})
            assert err.value.code == "invalid_category"
        listed = await suppliers.list_suppliers(ctx)
        assert listed["categories"] == ["flight", "insurance", "visa", "other"]
        air = uuid.UUID((await suppliers.create_supplier(ctx, vault, {"name": "Gull Air", "category": "flight"}))[
            "agency_supplier_id"
        ])  # fmt: skip
        body = {"agency_supplier_id": air, "title": "Gull Air DAD-SGN"}
        for category in ("transfer", "other", None):
            with pytest.raises(Invalid) as err:
                await lines.create_line(ctx, s["trip"], {**body, "category": category})
            assert err.value.code == "invalid_category"
        made = await lines.create_line(ctx, s["trip"], {**body, "category": "flight"})
        assert made["category"] == "flight"
        line_id = uuid.UUID(made["booking_line_id"])
        assert (await lines.update_line(ctx, line_id, {"category": "visa"}))["category"] == "visa"
        with pytest.raises(Invalid) as err:
            await lines.update_line(ctx, line_id, {"category": "transfer"})
        assert err.value.code == "invalid_category"
        with pytest.raises(Invalid) as err:
            await catalogue.create_service(ctx, {"category": "transfer", "name": "Harbour shuttle", "unit_sell": "20",
                                                 "currency": "USD", "qty_basis": "per_booking"})  # fmt: skip
        assert err.value.code == "invalid_category"


async def test_ask_aa_for_extra_nights_reaches_aa_ops(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    other = await _setup(app_pool)
    check_in = s["start"] - dt.timedelta(days=2)
    body = {"position": "before", "place": "Lantern Bay", "check_in": check_in.isoformat(), "nights": 2, "rooms": 1,
            "adults": 2, "children": 1}  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        made = await extra_nights.create(ctx, s["trip"], body)
        for bad, code in (({"nights": 0}, "invalid_nights"), ({"place": " "}, "invalid_place"),
                          ({"position": "during"}, "invalid_position"), ({"rooms": 31}, "invalid_rooms")):  # fmt: skip
            with pytest.raises(Invalid) as err:
                await extra_nights.create(ctx, s["trip"], {**body, **bad})
            assert err.value.code == code
    assert made["status"] == "open" and made["check_out"] == s["start"].isoformat() and made["children"] == 1
    # AA ground operations see it (place, dates, counts; the agency name and a trip reference), not another agency's.
    board = await extra_nights.staff_board(app_pool, _ops())
    mine = [i for i in board["items"] if i["request_id"] == made["request_id"]]
    assert len(mine) == 1 and mine[0]["tenant_id"] == str(s["tenant"]) and mine[0]["trip_ref"] == str(s["trip"])[:8]
    assert set(mine[0]) >= {"agency_name", "place", "check_in", "nights", "rooms", "adults", "children"}
    async with tenant_tx(app_pool, other["who"]["agent"], other["tenant"]) as ctx:
        assert (await extra_nights.list_for_trip(ctx, other["trip"]))["requests"] == []
    # The agent closes it once AA quoted (the AA hotel room is then added from the AA drawer); closed stays closed.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        closed = await extra_nights.close(ctx, uuid.UUID(made["request_id"]), "quoted")
        assert closed["status"] == "closed" and closed["close_reason"] == "quoted"
        with pytest.raises(Conflict) as err:
            await extra_nights.close(ctx, uuid.UUID(made["request_id"]), "not_needed")
        assert err.value.code == "request_closed"
        assert [r["status"] for r in (await extra_nights.list_for_trip(ctx, s["trip"]))["requests"]] == ["closed"]
    board = await extra_nights.staff_board(app_pool, _ops())
    assert all(i["request_id"] != made["request_id"] for i in board["items"])
