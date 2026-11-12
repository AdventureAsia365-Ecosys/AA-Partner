"""AA-1070 item crm-35: the inclusion lists without a database (the services run on PostgreSQL in
tests/integration/test_aa1070_inclusions_api.py, the schema in test_crm059_schema.py).

- Pure helpers: typed text, the CIS tour lists (strings and exclusion objects, empty and repeated lines dropped,
  long lines cut), the AA tours of the chosen options, the agency lists per kind, the copied AA block, the hash
  record, the line in a locale.
- The library and a draft's replace on a scripted connection: what is written, what is refused and how it is worded.
- The text block (e-mail / WhatsApp / PDF) with both lists and an AA tour.
- The routes: every route reaches its helper; the assistant reads and picks but never edits the library; bodies are
  checked before any helper runs.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.errors import Conflict, Invalid, NotFound
from app.main import create_app
from app.proposals import builder, inclusions
from app.proposals import text_render as tr
from app.routers import inclusions as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

P = "00000000-0000-4000-8000-0000000000c1"
I1 = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
I2 = uuid.UUID("00000000-0000-4000-8000-0000000000d2")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
PID = uuid.UUID(P)


def _row(**over: Any) -> dict[str, Any]:
    base = {"inclusion_item_id": I1, "kind": "included", "text_en": "Daily breakfast", "text_vi": None,
            "sort_order": 0, "archived": False, "updated_at": None}  # fmt: skip
    return base | over


def _line(**over: Any) -> dict[str, Any]:
    base = {"proposal_inclusion_id": uuid.uuid4(), "kind": "included", "source": "custom", "inclusion_item_id": None,
            "aa_tour_source_id": None, "aa_tour_title": None, "text_en": "Welcome dinner", "text_vi": None,
            "sort_order": 0}  # fmt: skip
    return base | over


# ── Pure helpers ─────────────────────────────────────────────────────────────────────────────────


def test_typed_text_is_one_trimmed_line() -> None:
    assert inclusions.clean_text("  Airport   pickup \n on day 1 ", "text_en") == "Airport pickup on day 1"
    assert inclusions.clean_text(None, "text_vi", required=False) is None
    assert inclusions.clean_text("   ", "text_vi", required=False) is None
    for bad in (None, "", "   ", "x" * 301, 7):
        with pytest.raises(Invalid) as err:
            inclusions.clean_text(bad, "text_en")
        assert err.value.code == "invalid_text_en"
    assert inclusions.kind_of("not_included") == "not_included"
    with pytest.raises(Invalid):
        inclusions.kind_of("excluded")


def test_the_cis_tour_lists() -> None:
    included, excluded = inclusions.aa_lists(
        '["Guide", " Guide ", "", null, "Boat\\tride"]',
        [{"code": "tips", "text": "Tips", "suggests_collect_on_arrival": False}, "Flights", {"code": "x"}],
    )
    assert included == ["Guide", "Boat ride"] and excluded == ["Tips", "Flights"]
    assert inclusions.aa_lists(None, {"not": "a list"}) == ([], [])
    many, _ = inclusions.aa_lists([f"Line {n}" for n in range(60)], [])
    assert len(many) == inclusions.MAX_AA_LINES
    (long,), _ = inclusions.aa_lists(["y" * 900], [])
    assert len(long) == inclusions.AA_TEXT and long.endswith("…")


def test_the_aa_tours_of_the_chosen_options() -> None:
    s = {
        "blocks": [{"option_block_id": "b1"}, {"option_block_id": "b2"}, {"option_block_id": "b3"}],
        "options": [
            {"option_id": "o1", "source": "aa", "aa_tour_source_id": "T-2"},
            {"option_id": "o2", "source": "aa", "aa_tour_source_id": "T-1"},
            {"option_id": "o3", "source": "agency", "aa_tour_source_id": None},
            {"option_id": "o4", "source": "aa", "aa_tour_source_id": "T-2"},
        ],
    }
    assert inclusions.tours_in(s, {"b1": "o1", "b2": "o3", "b3": "o4"}) == ["T-2"]
    assert inclusions.tours_in(s, {"b1": "o2", "b2": None, "b3": "o1"}) == ["T-1", "T-2"]
    assert inclusions.all_aa_tours(s) == ["T-2", "T-1"]


def test_lists_hash_and_locale() -> None:
    rows = [
        _line(kind="not_included", text_en="Flights", sort_order=0),
        _line(text_en="Dinner", text_vi="Bữa tối", sort_order=1),
        _line(text_en="Guide", sort_order=0, source="library", inclusion_item_id=I1),
        _line(source="aa_tour", aa_tour_source_id="T-1", aa_tour_title="Loop", text_en="Boat", sort_order=0),
        _line(source="aa_tour", kind="not_included", aa_tour_source_id="T-1", aa_tour_title="Loop", text_en="Tips"),
    ]
    own = inclusions.agency_lists(rows)
    assert [x["text_en"] for x in own["included"]] == ["Guide", "Dinner"]
    assert own["not_included"] == [{"text_en": "Flights", "text_vi": None}]
    assert inclusions.aa_from_rows(rows, ["T-9", "T-1"]) == [
        {"source_id": "T-1", "title": "Loop", "included": ["Boat"], "not_included": ["Tips"]}]  # fmt: skip
    assert inclusions.aa_from_rows(rows, []) == []
    record = inclusions.hash_record(rows)
    assert len(record) == 5 and record == sorted(record)
    assert [str(I1)] == [r[2] for r in record if r[2]]
    assert inclusions.text_of({"text_en": "Dinner", "text_vi": "Bữa tối"}, "vi") == "Bữa tối"
    assert inclusions.text_of({"text_en": "Dinner", "text_vi": None}, "vi") == "Dinner"
    assert inclusions.text_of({"text_en": "Dinner", "text_vi": "Bữa tối"}, "en") == "Dinner"


def test_the_text_block_lists_both_kinds_and_the_aa_tour() -> None:
    snap = tr.Snapshot(
        kind="builder", version=2, price_hash="ab" * 32, agency_name="Lotus Trails", trip_title="Lakes",
        currency="USD", total=Decimal("100"), deposit=Decimal("30"), balance_due_on=None,
        included=("Daily breakfast",), not_included=(),
        aa_tours=(tr.TourLists("Lantern Valley Loop", ("Local guide",), ("Tips",)),),
    )  # fmt: skip
    body = tr.render(snap, "email", "en").body
    assert "Included\n- Daily breakfast\nLantern Valley Loop:\n- Local guide" in body
    assert "Not included\nLantern Valley Loop:\n- Tips" in body
    assert body.index("Not included") < body.index("Price")
    bare = tr.render(tr.Snapshot(kind="builder", version=1, price_hash="ab" * 32, agency_name="A", trip_title="T",
                                 currency="USD", total=Decimal("1"), deposit=Decimal("1"), balance_due_on=None),
                     "whatsapp", "vi").body  # fmt: skip
    assert "Đã bao gồm" not in bare and "Chưa bao gồm" not in bare
    vi = tr.render(snap, "whatsapp", "vi").body
    assert "*Đã bao gồm*" in vi and "*Chưa bao gồm*" in vi


# ── The library on a scripted connection ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_writes_the_next_order_and_words_refusals() -> None:
    conn = ScriptedConn().on("SELECT count(*)", 3).on("COALESCE(max(sort_order)", 4).on("WHERE inclusion_item_id = $1",
                                                                                        _row(sort_order=4))  # fmt: skip
    ctx = tenant_ctx(conn, role="agent")
    out = await inclusions.create_item(ctx, {"kind": "included", "text_en": " Daily  breakfast", "text_vi": ""})
    assert out["sort_order"] == 4 and out["text_en"] == "Daily breakfast"
    (args,) = conn.sql("INSERT INTO crm.inclusion_item")
    assert args[1:] == ("included", "Daily breakfast", None, 4)
    assert conn.sql("INSERT INTO crm.tenant_settings")
    full = tenant_ctx(ScriptedConn().on("SELECT count(*)", inclusions.MAX_ITEMS), role="agent")
    with pytest.raises(Conflict) as err:
        await inclusions.create_item(full, {"kind": "included", "text_en": "Tea"})
    assert err.value.code == "inclusion_library_full"
    dup = tenant_ctx(ScriptedConn().on("INSERT INTO crm.inclusion_item", asyncpg.UniqueViolationError("dup")))
    with pytest.raises(Conflict) as err:
        await inclusions.create_item(dup, {"kind": "included", "text_en": "Tea", "sort_order": 2})
    assert err.value.code == "inclusion_item_exists"
    bad = tenant_ctx(ScriptedConn().on("INSERT INTO crm.inclusion_item", asyncpg.CheckViolationError("chk")))
    with pytest.raises(Invalid):
        await inclusions.create_item(bad, {"kind": "included", "text_en": "Tea", "sort_order": 2})
    with pytest.raises(Invalid):
        await inclusions.create_item(tenant_ctx(ScriptedConn()), {"kind": "included", "text_en": "Tea",
                                                                  "sort_order": 1000})  # fmt: skip
    other = tenant_ctx(ScriptedConn().on("INSERT INTO crm.inclusion_item", asyncpg.DataError("x")))
    with pytest.raises(asyncpg.DataError):
        await inclusions.create_item(other, {"kind": "included", "text_en": "Tea", "sort_order": 1})


@pytest.mark.asyncio
async def test_update_changes_only_the_sent_fields_and_delete() -> None:
    conn = ScriptedConn().on("WHERE inclusion_item_id = $1", _row(text_vi="Bữa sáng"))
    ctx = tenant_ctx(conn, role="owner")
    await inclusions.update_item(ctx, I1, {"archived": True})
    (args,) = conn.sql("UPDATE crm.inclusion_item")
    assert args == (I1, "included", "Daily breakfast", "Bữa sáng", 0, True)
    await inclusions.update_item(ctx, I1, {"text_vi": None, "kind": "not_included", "sort_order": 9})
    assert conn.sql("UPDATE crm.inclusion_item")[-1] == (I1, "not_included", "Daily breakfast", None, 9, False)
    with pytest.raises(Invalid):
        await inclusions.update_item(ctx, I1, {"archived": "yes"})
    with pytest.raises(NotFound):
        await inclusions.update_item(tenant_ctx(ScriptedConn()), I1, {"archived": True})
    dup = ScriptedConn().on("WHERE inclusion_item_id = $1", _row()).on("UPDATE crm.inclusion_item",
                                                                       asyncpg.UniqueViolationError("dup"))  # fmt: skip
    with pytest.raises(Conflict):
        await inclusions.update_item(tenant_ctx(dup), I1, {"text_en": "Tea"})
    gone = ScriptedConn().on("DELETE FROM crm.inclusion_item", I1)
    assert (await inclusions.delete_item(tenant_ctx(gone), I1)) == {"inclusion_item_id": str(I1), "deleted": True}
    with pytest.raises(NotFound):
        await inclusions.delete_item(tenant_ctx(ScriptedConn()), I1)
    listed = await inclusions.library(tenant_ctx(ScriptedConn().on("FROM crm.inclusion_item", [_row()])))
    assert listed["items"][0]["inclusion_item_id"] == str(I1) and listed["kinds"] == ["included", "not_included"]


@pytest.mark.asyncio
async def test_replace_keeps_copies_picks_library_lines_and_types() -> None:
    kept = _line(source="library", inclusion_item_id=I1, text_en="Old breakfast", text_vi="Sáng")
    conn = (
        ScriptedConn()
        .on("FROM crm.proposal_inclusion WHERE proposal_id", [kept])
        .on("WHERE inclusion_item_id = ANY", [
            {"inclusion_item_id": I1, "kind": "included", "text_en": "New", "text_vi": None, "archived": True},
            {"inclusion_item_id": I2, "kind": "not_included", "text_en": "Flights", "text_vi": None,
             "archived": False},
        ])
    )  # fmt: skip
    ctx = tenant_ctx(conn)
    n = await inclusions.replace(ctx, PID, TRIP, [
        {"kind": "included", "text_en": "Dinner", "text_vi": "Tối"},
        {"inclusion_item_id": str(I1)},
        {"inclusion_item_id": I2},
        {"inclusion_item_id": I2},
    ])  # fmt: skip
    assert n == 3
    assert conn.sql("DELETE FROM crm.proposal_inclusion WHERE proposal_id = $1 AND source <> 'aa_tour'")
    inserted = [(a[2], a[3], a[7], a[9]) for a in conn.sql("INSERT INTO crm.proposal_inclusion")]
    assert inserted == [("included", "custom", "Dinner", 0), ("included", "library", "Old breakfast", 1),
                        ("not_included", "library", "Flights", 0)]  # fmt: skip
    with pytest.raises(Invalid) as err:
        await inclusions.replace(ctx, PID, TRIP, [{"kind": "included", "text_en": "x"}] * (inclusions.MAX_LINES + 1))
    assert err.value.code == "too_many_lines"
    with pytest.raises(Invalid):
        await inclusions.replace(ctx, PID, TRIP, [{"inclusion_item_id": "not-an-id"}])
    archived = ScriptedConn().on("WHERE inclusion_item_id = ANY", [{"inclusion_item_id": I1, "kind": "included",
                                                                    "text_en": "New", "text_vi": None,
                                                                    "archived": True}])  # fmt: skip
    with pytest.raises(Conflict) as err:
        await inclusions.replace(tenant_ctx(archived), PID, TRIP, [{"inclusion_item_id": I1}])
    assert err.value.code == "inclusion_item_archived"
    with pytest.raises(NotFound):
        await inclusions.replace(tenant_ctx(ScriptedConn()), PID, TRIP, [{"inclusion_item_id": I2}])


@pytest.mark.asyncio
async def test_copy_snapshot_and_the_views() -> None:
    rows = [_line(text_en="Dinner"), _line(source="aa_tour", aa_tour_source_id="T-1", aa_tour_title="Loop",
                                           text_en="Boat")]  # fmt: skip
    tour = {"source_id": "T-1", "title": "  Lantern   Loop ", "inclusions": ["Guide"], "exclusions": [{"text": "Tips"}]}
    conn = ScriptedConn().on("FROM crm.proposal_inclusion WHERE proposal_id", rows).on("FROM crm.aa_catalog_tour",
                                                                                       [tour])  # fmt: skip
    ctx = tenant_ctx(conn)
    await inclusions.copy(ctx, PID, uuid.uuid4(), TRIP)
    (copied,) = conn.sql("INSERT INTO crm.proposal_inclusion")
    assert copied[7] == "Dinner" and copied[10] == rows[0]["proposal_inclusion_id"]
    s = {"blocks": [{"option_block_id": "b1"}], "options": [{"option_id": "o1", "source": "aa",
                                                            "aa_tour_source_id": "T-1"}]}  # fmt: skip
    assert await inclusions.snapshot_aa(ctx, PID, TRIP, s) == 2
    snap = [(a[2], a[5], a[6], a[7]) for a in conn.sql("INSERT INTO crm.proposal_inclusion")[1:]]
    assert snap == [("included", "T-1", "Lantern Loop", "Guide"), ("not_included", "T-1", "Lantern Loop", "Tips")]
    # A draft reads the cache, a published version its copy.
    draft = await inclusions.client_view(ctx, PID, "draft", ["T-1"])
    assert draft["aa_tours"][0]["included"] == ["Guide"] and draft["included"][0]["text_en"] == "Dinner"
    sent = await inclusions.client_view(ctx, PID, "sent", ["T-1"])
    assert sent["aa_tours"] == [{"source_id": "T-1", "title": "Loop", "included": ["Boat"], "not_included": []}]
    assert await inclusions.aa_live(ctx, []) == []
    assert await inclusions.aa_live(tenant_ctx(ScriptedConn()), ["T-404"]) == []
    panel = await inclusions.builder_view(ctx, PID, "draft", s, {"b1": "o1"})
    assert [x["text_en"] for x in panel["lines"]] == ["Dinner"] and panel["max_lines"] == inclusions.MAX_LINES


@pytest.mark.asyncio
async def test_builder_inclusions_view_refuses_a_missing_or_text_version() -> None:
    with pytest.raises(NotFound):
        await builder.inclusions_view(tenant_ctx(ScriptedConn()), PID)
    text = ScriptedConn().on("FROM crm.proposal p JOIN crm.trip", {"kind": "text", "status": "draft"})
    with pytest.raises(Conflict) as err:
        await builder.inclusions_view(tenant_ctx(text), PID)
    assert err.value.code == "proposal_not_builder"


# ── Routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    out: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            out.append((name, args, kwargs))
            return {"ok": name}

        return fn

    for name in ("library", "create_item", "update_item", "delete_item"):
        monkeypatch.setattr(inclusions, name, stub(name))
    for name in ("inclusions_view", "set_inclusions"):
        monkeypatch.setattr(builder, name, stub(name))
    return out


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn(), role=role)))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    return TestClient(app), {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    item = f"/crm/v1/inclusion-items/{I1}"
    requests = [
        ("get", "/crm/v1/inclusion-items?archived=true", None, "library", 200),
        ("post", "/crm/v1/inclusion-items", {"kind": "included", "text_en": "Tea"}, "create_item", 201),
        ("patch", item, {"text_vi": None, "archived": True, "text_en": None}, "update_item", 200),
        ("delete", item, None, "delete_item", 200),
        ("get", f"/crm/v1/proposals/{P}/inclusions", None, "inclusions_view", 200),
        ("put", f"/crm/v1/proposals/{P}/inclusions", {"seq": 4, "lines": [
            {"inclusion_item_id": str(I1)}, {"kind": "not_included", "text_en": "Flights"}]}, "set_inclusions", 200),
    ]  # fmt: skip
    for method, path, body, helper, status in requests:
        r = getattr(client, method)(path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == status, (path, r.text)
        assert calls[-1][0] == helper, path
    assert calls[0][2] == {"archived": True}
    patch = next(c for c in calls if c[0] == "update_item")
    assert patch[1][2] == {"text_vi": None, "archived": True}  # a null English text keeps it, a null VI clears it
    put = calls[-1][1]
    assert put[3] == 4 and put[4] == [{"inclusion_item_id": I1}, {"kind": "not_included", "text_en": "Flights"}]


def test_the_assistant_reads_and_picks_but_never_edits_the_library(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    item = f"/crm/v1/inclusion-items/{I1}"
    assert (
        client.post("/crm/v1/inclusion-items", headers=h, json={"kind": "included", "text_en": "Tea"}).status_code
        == 403
    )
    assert client.patch(item, headers=h, json={"archived": True}).status_code == 403
    assert client.delete(item, headers=h).status_code == 403
    assert calls == []
    assert client.get("/crm/v1/inclusion-items", headers=h).status_code == 200
    r = client.put(f"/crm/v1/proposals/{P}/inclusions", headers=h, json={"seq": 1, "lines": []})
    assert r.status_code == 200 and calls[-1][0] == "set_inclusions"


def test_bodies_are_checked_first(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    for body in ({"kind": "maybe", "text_en": "Tea"}, {"kind": "included", "text_en": ""},
                 {"kind": "included", "text_en": "x" * 301}, {"kind": "included", "text_en": "Tea", "extra": 1},
                 {"kind": "included", "text_en": "Tea", "sort_order": 1000}):  # fmt: skip
        assert client.post("/crm/v1/inclusion-items", headers=h, json=body).status_code == 422, body
    put = f"/crm/v1/proposals/{P}/inclusions"
    assert client.put(put, headers=h, json={"lines": []}).status_code == 422  # no seq
    many = [{"kind": "included", "text_en": "x"}] * (inclusions.MAX_LINES + 1)
    assert client.put(put, headers=h, json={"seq": 1, "lines": many}).status_code == 422
    assert client.put(put, headers=h, json={"seq": 1, "lines": [{"kind": "other", "text_en": "x"}]}).status_code == 422
    assert calls == []


@pytest.mark.asyncio
async def test_set_inclusions_locks_the_draft_then_answers_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def lock(ctx: Any, proposal_id: Any, seq: Any) -> dict[str, Any]:
        seen.append(("lock", proposal_id, seq))
        return {"trip_id": TRIP}

    async def replace(ctx: Any, proposal_id: Any, trip_id: Any, entries: Any) -> int:
        seen.append(("replace", trip_id, entries))
        return len(entries)

    async def done(ctx: Any, vault: Any, proposal_id: Any, reason: str) -> dict[str, Any]:
        seen.append(("done", reason))
        return {"proposal_id": str(proposal_id)}

    monkeypatch.setattr(builder, "_lock", lock)
    monkeypatch.setattr(inclusions, "replace", replace)
    monkeypatch.setattr(builder, "_done", done)
    out = await builder.set_inclusions(tenant_ctx(ScriptedConn()), FakeVault(), PID, 7, [{"inclusion_item_id": I1}])
    assert out == {"proposal_id": P}
    assert seen == [("lock", PID, 7), ("replace", TRIP, [{"inclusion_item_id": I1}]), ("done", "inclusions")]
