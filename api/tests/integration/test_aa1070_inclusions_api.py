"""AA-1070 item crm-35 end to end on PostgreSQL with the RDS role shape (needs crm-059): the agency's inclusion library
and the "Included" / "Not included" lines of a builder version, as crm_app through ``tenant_tx()``.

- The library: add, refuse a second line with the same text, archive, delete; another agency sees none of it.
- A draft picks library lines and types its own, reorders and unpicks them with one replace (the version counter);
  an archived library line stays on a draft that has it but is not picked anew; the AA tour's lists come from the
  catalogue cache, read-only, for the default option.
- Publish copies the AA tour's lists into the version and covers the lines with the price hash; the published version
  never changes (CRM35 -> 409 ``proposal_not_draft``) and keeps its lists when the library or the CIS content
  changes; a working copy carries the agency's lines.
- One section, three forms: the client page, the e-mail / WhatsApp text (and so the proposal PDF) show the same lists,
  Vietnamese where the line has it, English otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.errors import Conflict, Invalid, NotFound
from app.proposals import builder, inclusions, text_proposal
from app.proposals import public as page
from app.tenancy import tenant_tx
from tests.aa_fakes import FakeBooking
from tests.integration.test_builder_api import _aa, _as, _block, _create, _option, _pid, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "T-1070-35"
BODY = {
    "inclusions": ["Local guide", "Boat ride on the lake", "", "Local guide"],
    "exclusions": [{"code": "tips", "text": "Tips for the crew", "suggests_collect_on_arrival": False}],
}


async def _tour(pool: asyncpg.Pool, body: dict[str, Any] = BODY) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state, body) "
            "VALUES ($1, 'Lantern Valley Loop', 'LA', 4, 'served', $2::jsonb) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served', body = EXCLUDED.body",
            TOUR,
            json.dumps(body),
        )


@pytest_asyncio.fixture(autouse=True)
async def _drop_tour(admin: asyncpg.Connection) -> Any:
    # crm.aa_catalog_tour is shared by every test of the run: a served row left behind is one more tour the catalog
    # sync re-checks in a later test (test_aa_lines_api counts the CIS fetches; S278 shard 1 red, 5 == 2 + 2).
    yield
    # crm_app has no DELETE on the catalogue cache (only the sync writes it): the owner connection removes the row.
    await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1", TOUR)


async def _lib(pool: asyncpg.Pool, s: dict[str, Any], fn: Any, *args: Any, role: str = "agent", **kw: Any) -> Any:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await fn(ctx, *args, **kw)


async def _draft(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    v = await _create(pool, s)
    v = await _aa(pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id=TOUR, sell_amount=Decimal("1200"))
    v = await _option(pool, s, v, _block(v, 1), title="Guesthouse", sell_amount=Decimal("1100"))
    return await _as(pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                     {"balance_due_on": s["start"] - dt.timedelta(days=30)})  # fmt: skip


def _texts(view: dict[str, Any], kind: str) -> list[str]:
    return [line["text_en"] for line in view["inclusions"]["lines"] if line["kind"] == kind]


async def test_the_library_and_its_isolation(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    other = await _setup(app_pool)
    body = {"kind": "not_included", "text_en": " International   flights ", "text_vi": "Vé máy bay"}
    flights = await _lib(app_pool, s, inclusions.create_item, body)
    assert flights["text_en"] == "International flights" and flights["sort_order"] == 0 and not flights["archived"]
    second = await _lib(app_pool, s, inclusions.create_item, {"kind": "not_included", "text_en": "Visa fees"})
    assert second["sort_order"] == 1
    with pytest.raises(Conflict) as err:
        await _lib(app_pool, s, inclusions.create_item, {"kind": "not_included", "text_en": "international FLIGHTS"})
    assert err.value.code == "inclusion_item_exists"
    with pytest.raises(Invalid):
        await _lib(app_pool, s, inclusions.create_item, {"kind": "maybe", "text_en": "Water"})
    item_id = uuid.UUID(flights["inclusion_item_id"])
    changed = await _lib(app_pool, s, inclusions.update_item, item_id, {"archived": True, "text_vi": None})
    assert changed["archived"] and changed["text_vi"] is None
    offered = await _lib(app_pool, s, inclusions.library)
    assert [i["text_en"] for i in offered["items"]] == ["Visa fees"]
    every = await _lib(app_pool, s, inclusions.library, archived=True)
    assert len(every["items"]) == 2
    assert (await _lib(app_pool, other, inclusions.library, archived=True))["items"] == []
    with pytest.raises(NotFound):
        await _lib(app_pool, other, inclusions.update_item, item_id, {"archived": False})
    with pytest.raises(NotFound):
        await _lib(app_pool, other, inclusions.delete_item, item_id)
    assert (await _lib(app_pool, s, inclusions.delete_item, item_id))["deleted"] is True


async def test_a_draft_picks_types_and_reorders_then_publish_freezes_the_lists(app_pool: asyncpg.Pool) -> None:
    await _tour(app_pool)
    s = await _setup(app_pool)
    meals = await _lib(app_pool, s, inclusions.create_item,
                       {"kind": "included", "text_en": "Daily breakfast", "text_vi": "Bữa sáng hằng ngày"})  # fmt: skip
    flights = await _lib(app_pool, s, inclusions.create_item, {"kind": "not_included", "text_en": "Long-haul flights"})
    v = await _draft(app_pool, s)
    panel = v["inclusions"]
    assert panel["lines"] == [] and {i["text_en"] for i in panel["library"]} == {"Daily breakfast", "Long-haul flights"}
    assert panel["aa_tours"] == [{"source_id": TOUR, "title": "Lantern Valley Loop",
                                  "included": ["Local guide", "Boat ride on the lake"],
                                  "not_included": ["Tips for the crew"]}]  # fmt: skip
    seq = v["edit_seq"]
    v = await _as(app_pool, s, "assistant", builder.set_inclusions, _pid(v), seq, [
        {"kind": "included", "text_en": "Welcome dinner"},
        {"inclusion_item_id": meals["inclusion_item_id"]},
        {"inclusion_item_id": flights["inclusion_item_id"]},
        {"inclusion_item_id": meals["inclusion_item_id"]},  # a repeat is one line
    ])  # fmt: skip
    assert v["edit_seq"] == seq + 1
    assert _texts(v, "included") == ["Welcome dinner", "Daily breakfast"]
    assert _texts(v, "not_included") == ["Long-haul flights"]
    with pytest.raises(Conflict) as stale:
        await _as(app_pool, s, "agent", builder.set_inclusions, _pid(v), seq, [])
    assert stale.value.code == "version_conflict"
    # The library line is archived and edited: the draft keeps its copy on a reorder, a new pick is refused.
    await _lib(app_pool, s, inclusions.update_item, uuid.UUID(meals["inclusion_item_id"]),
               {"archived": True, "text_en": "Breakfast"})  # fmt: skip
    v = await _as(app_pool, s, "agent", builder.set_inclusions, _pid(v), v["edit_seq"], [
        {"inclusion_item_id": meals["inclusion_item_id"]},
        {"kind": "included", "text_en": "Welcome dinner", "text_vi": "Bữa tối chào mừng"},
        {"inclusion_item_id": flights["inclusion_item_id"]},
    ])  # fmt: skip
    assert _texts(v, "included") == ["Daily breakfast", "Welcome dinner"]
    other_meal = await _lib(app_pool, s, inclusions.create_item, {"kind": "included", "text_en": "Packed lunch"})
    await _lib(app_pool, s, inclusions.update_item, uuid.UUID(other_meal["inclusion_item_id"]), {"archived": True})
    with pytest.raises(Conflict) as archived:
        await _as(app_pool, s, "agent", builder.set_inclusions, _pid(v), v["edit_seq"],
                  [{"inclusion_item_id": other_meal["inclusion_item_id"]}])  # fmt: skip
    assert archived.value.code == "inclusion_item_archived"
    with pytest.raises(NotFound):
        await _as(app_pool, s, "agent", builder.set_inclusions, _pid(v), v["edit_seq"],
                  [{"inclusion_item_id": str(uuid.uuid4())}])  # fmt: skip
    with pytest.raises(Invalid):
        await _as(app_pool, s, "agent", builder.set_inclusions, _pid(v), v["edit_seq"],
                  [{"kind": "included", "text_en": "x" * 301}])  # fmt: skip

    pub = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert pub["status"] == "published"
    with pytest.raises(Conflict) as sealed:
        await _as(app_pool, s, "agent", builder.set_inclusions, _pid(pub), pub["edit_seq"], [])
    assert sealed.value.code == "proposal_not_draft"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        rows = await inclusions.lines(ctx, _pid(pub))
    assert sorted(r["text_en"] for r in rows if r["source"] == "aa_tour") == [
        "Boat ride on the lake", "Local guide", "Tips for the crew"]  # fmt: skip
    # CIS changes the tour and the library line goes: the published version keeps what it went out with.
    await _tour(app_pool, {"inclusions": ["Something new"], "exclusions": []})
    await _lib(app_pool, s, inclusions.delete_item, uuid.UUID(flights["inclusion_item_id"]))
    got = await _lib(app_pool, s, builder.inclusions_view, _pid(pub))
    assert got["status"] == "published" and got["aa_tours"][0]["included"] == ["Local guide", "Boat ride on the lake"]
    assert [x["text_en"] for x in got["lines"]] == ["Daily breakfast", "Welcome dinner", "Long-haul flights"]

    # The e-mail text (and so the PDF) in Vietnamese: the Vietnamese copy where the line has one.
    vi = await _as(app_pool, s, "agent", text_proposal.preview, _pid(pub), fmt="email", locale="vi")
    assert "Đã bao gồm" in vi["body"] and "- Bữa sáng hằng ngày" in vi["body"] and "- Bữa tối chào mừng" in vi["body"]
    assert "Chưa bao gồm" in vi["body"] and "- Long-haul flights" in vi["body"]
    assert "Lantern Valley Loop:\n- Local guide" in vi["body"] and "Something new" not in vi["body"]
    en = await _as(app_pool, s, "agent", text_proposal.preview, _pid(pub), fmt="whatsapp", locale="en")
    assert "*Included*" in en["body"] and "- Daily breakfast" in en["body"] and "*Not included*" in en["body"]

    # The client page shows the same lists, both languages, no ids.
    sent = await _as(app_pool, s, "agent", text_proposal.send, _pid(pub), fmt="whatsapp", locale="en",
                     price_hash=pub["price_hash"], client_origin="https://go.example.test")  # fmt: skip
    client = await page.read(app_pool, s["keys"], sent["link_path"].rsplit("/", 1)[1])
    lists = client["inclusions"]
    assert lists["included"] == [{"text_en": "Daily breakfast", "text_vi": "Bữa sáng hằng ngày"},
                                 {"text_en": "Welcome dinner", "text_vi": "Bữa tối chào mừng"}]  # fmt: skip
    assert lists["not_included"] == [{"text_en": "Long-haul flights", "text_vi": None}]
    assert lists["aa_tours"][0]["not_included"] == ["Tips for the crew"]
    assert "inclusion_item_id" not in json.dumps(lists)

    # A working copy carries the agency's lines (not the AA copy: Publish makes it again).
    v2 = await _create(app_pool, s, from_proposal_id=_pid(pub))
    assert _texts(v2, "included") == ["Daily breakfast", "Welcome dinner"]
    assert v2["inclusions"]["aa_tours"][0]["included"] == ["Something new"]  # a draft reads the cache


async def test_the_price_hash_covers_the_lines(app_pool: asyncpg.Pool) -> None:
    await _tour(app_pool)
    s = await _setup(app_pool)
    a = await _draft(app_pool, s)
    a = await _as(app_pool, s, "agent", builder.set_inclusions, _pid(a), a["edit_seq"],
                  [{"kind": "included", "text_en": "Airport pickup"}])  # fmt: skip
    plain = dict(a)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchrow(builder.PROPOSAL_SQL, _pid(a))
        st = await builder._structure(ctx, _pid(a))
        sums = builder._charged(st, None, row["deposit_pct"])
        rows = await inclusions.lines(ctx, _pid(a))
    with_lines = builder.price_hash(row, "", None, st, sums, rows)
    without = builder.price_hash(row, "", None, st, sums, [])
    assert with_lines != without and without == builder.price_hash(row, "", None, st, sums)
    assert plain["inclusions"]["lines"][0]["source"] == "custom"
