"""AA-960 (CRM-29b) tag service branches on a scripted connection: the vocabulary, create / rename / delete, one record,
bulk preview and apply (card E), the archived-client suppression (card R), the R36 list, the system-tag refresh and the
workflow coverage (R30). SQL, RLS and the CRM55 guards: tests/integration/test_aa960_tags_db.py and
test_crm061_schema.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.tags import model, ready, service, system
from app.workflows import coverage, triggers
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, FakeVault, enc, system_ctx, tenant_ctx

TAG = uuid.UUID("00000000-0000-4000-8000-00000000a960")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
CLIENT = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
LINK = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
RULE = uuid.UUID("00000000-0000-4000-8000-0000000000c1")

pytestmark = pytest.mark.asyncio


def tag_row(**over: Any) -> dict[str, Any]:
    base = {"tag_id": TAG, "kind": "agency", "applies_to": "trip", "code": "vip", "name": "VIP", "tone": "neutral",
            "archived_at": None}  # fmt: skip
    base.update(over)
    return base


class HoldError(asyncpg.PostgresError):
    sqlstate = "CRM10"


async def test_vocabulary_and_create() -> None:
    conn = ScriptedConn().on("FROM crm.tag g", [{**tag_row(), "uses": 3}])
    got = await service.vocabulary(tenant_ctx(conn, role="agent"), applies_to="trip")
    assert got["tags"][0] == {**{k: v for k, v in tag_row().items() if k != "archived_at"}, "tag_id": str(TAG),
                              "archived": False, "uses": 3}  # fmt: skip
    assert got["can_manage"] is False and got["can_bulk"] is True
    with pytest.raises(Invalid):
        await service.vocabulary(tenant_ctx(conn), applies_to="booking")

    conn = ScriptedConn().on("SELECT code, lower(name)", [{"code": "vip", "lname": "vip", "tag_id": TAG}])
    conn.on("SELECT count(*)::int FROM crm.tag", 1).on("INSERT INTO crm.tag", tag_row(code="vip_2", name="VIP 2"))
    ctx = tenant_ctx(conn, role="assistant")
    created = await service.create(ctx, "trip", " VIP  2 ", "info")
    assert created["code"] == "vip_2"
    assert conn.sql("INSERT INTO crm.tag")[0] == ("trip", "vip_2", "VIP 2", "info")
    assert ctx.audit_events[-1].action == "tag_create"
    with pytest.raises(Conflict) as err:
        await service.create(ctx, "trip", "vip")
    assert err.value.code == "tag_exists" and err.value.extra["tag_id"] == str(TAG)
    conn.on("SELECT count(*)::int FROM crm.tag", model.MAX_TAGS)
    with pytest.raises(Conflict):
        await service.create(ctx, "trip", "Another")


async def test_manage_is_the_owners_and_never_a_system_tag() -> None:
    conn = ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row()).on("SELECT code, lower(name)", [])
    conn.on("UPDATE crm.tag SET", tag_row(name="Very important", tone="purple", archived_at=dt.datetime.now(dt.UTC)))
    with pytest.raises(Forbidden):
        await service.update(tenant_ctx(conn, role="agent"), TAG, name="x")
    out = await service.update(tenant_ctx(conn), TAG, name="Very important", tone="purple", archived=True)
    assert out["archived"] is True
    assert conn.sql("UPDATE crm.tag SET")[-1] == (TAG, "Very important", "purple", True)
    await service.update(tenant_ctx(conn), TAG)  # nothing sent: keeps the name, tone and archived state
    assert conn.sql("UPDATE crm.tag SET")[-1] == (TAG, "VIP", "neutral", False)
    conn.on("SELECT code, lower(name)", [{"code": "gold", "lname": "gold", "tag_id": uuid.uuid4()}])
    with pytest.raises(Conflict):
        await service.update(tenant_ctx(conn), TAG, name="Gold")
    conn.on("FROM crm.tag WHERE tag_id", tag_row(kind="system", code="sys_aa_trekking"))
    for call in (service.update(tenant_ctx(conn), TAG, name="x"), service.delete(tenant_ctx(conn), TAG)):
        with pytest.raises(Conflict) as err:
            await call
        assert err.value.code == "system_tag"
    conn.on("FROM crm.tag WHERE tag_id", None)
    with pytest.raises(NotFound):
        await service.delete(tenant_ctx(conn), TAG)


async def test_delete_counts_links_and_maps_a_hold() -> None:
    conn = ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row()).on("SELECT count(*)::int FROM crm.tag_link", 4)
    ctx = tenant_ctx(conn)
    assert await service.delete(ctx, TAG) == {"tag_id": str(TAG), "deleted": True, "removed_links": 4}
    assert ctx.audit_events[-1].reason == "links_4"
    conn.on("DELETE FROM crm.tag WHERE", HoldError("held"))
    with pytest.raises(Conflict) as err:
        await service.delete(tenant_ctx(conn), TAG)
    assert err.value.code == "legal_hold"
    conn.on("DELETE FROM crm.tag WHERE", asyncpg.PostgresError("other"))
    with pytest.raises(asyncpg.PostgresError):
        await service.delete(tenant_ctx(conn), TAG)


async def test_one_record_put_and_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[Any, ...]] = []

    async def on_tag_added(ctx: Any, applies_to: str, code: str, subject: Any, link: Any, **kw: Any) -> int:
        started.append((applies_to, code, subject, link, kw["client_archived"]))
        return 0 if kw["client_archived"] else 1

    monkeypatch.setattr(triggers, "on_tag_added", on_tag_added)
    conn = (
        ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row()).on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP})
    )
    conn.on("INSERT INTO crm.tag_link (tag_id, trip_id)", LINK)
    out = await service.put(tenant_ctx(conn, role="assistant"), TAG, trip_id=TRIP)
    assert out == {"tag_id": str(TAG), "added": True, "workflows_started": 1, "workflows_suppressed": False}
    conn.on("INSERT INTO crm.tag_link (tag_id, trip_id)", None)
    assert (await service.put(tenant_ctx(conn), TAG, trip_id=TRIP))["added"] is False
    conn.on("FROM crm.trip WHERE trip_id", None)
    with pytest.raises(NotFound):
        await service.put(tenant_ctx(conn), TAG, trip_id=TRIP)
    # A client tag on an archived client: tagged, workflows suppressed (card R).
    conn = ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row(applies_to="client", code="repeat"))
    conn.on("FROM crm.traveler WHERE traveler_id", {"traveler_id": CLIENT, "archived": True})
    conn.on("INSERT INTO crm.tag_link (tag_id, traveler_id)", LINK)
    out = await service.put(tenant_ctx(conn), TAG, traveler_id=CLIENT)
    assert out["workflows_suppressed"] is True and started[-1] == ("client", "repeat", CLIENT, LINK, True)
    with pytest.raises(Invalid) as err:
        await service.put(tenant_ctx(conn), TAG, trip_id=TRIP)
    assert err.value.code == "tag_subject_mismatch"
    conn.on("FROM crm.traveler WHERE traveler_id", None)
    with pytest.raises(NotFound):
        await service.put(tenant_ctx(conn), TAG, traveler_id=CLIENT)
    for row, code in ((tag_row(kind="system", code="sys_x"), "system_tag"),
                      (tag_row(archived_at=dt.datetime.now(dt.UTC)), "tag_archived")):  # fmt: skip
        conn.on("FROM crm.tag WHERE tag_id", row)
        with pytest.raises(Conflict) as cerr:
            await service.put(tenant_ctx(conn), TAG, trip_id=TRIP)
        assert cerr.value.code == code
    # Remove.
    conn = (
        ScriptedConn()
        .on("FROM crm.tag WHERE tag_id", tag_row())
        .on("DELETE FROM crm.tag_link", [{"tag_link_id": LINK}])
    )
    ctx = tenant_ctx(conn)
    assert await service.remove(ctx, TAG, trip_id=TRIP) == {"tag_id": str(TAG), "removed": True}
    assert ctx.audit_events[-1].action == "tag_remove"
    conn.on("DELETE FROM crm.tag_link", HoldError("held"))
    with pytest.raises(Conflict):
        await service.remove(ctx, TAG, trip_id=TRIP)
    conn.on("FROM crm.tag WHERE tag_id", tag_row(kind="system"))
    with pytest.raises(Conflict):
        await service.remove(ctx, TAG, trip_id=TRIP)


async def test_tags_of_one_record_and_of_many() -> None:
    now = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    row = {"tag_link_id": LINK, "source": "system", "reasons": json.dumps([{"code": "aa_tour", "tour": "T-1"}]),
           "created_at": now, **tag_row(kind="system", code="sys_aa_trekking")}  # fmt: skip
    conn = ScriptedConn().on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP}).on("FROM crm.tag_link l JOIN", [row])
    got = await service.of_subject(tenant_ctx(conn), trip_id=TRIP)
    assert got["tags"][0]["reasons"] == [{"code": "aa_tour", "tour": "T-1"}] and got["tags"][0]["source"] == "system"
    with pytest.raises(Invalid):
        await service.of_subject(tenant_ctx(conn))
    conn.on("FROM crm.traveler WHERE traveler_id", None)
    with pytest.raises(NotFound):
        await service.of_subject(tenant_ctx(conn), traveler_id=CLIENT)
    conn = ScriptedConn().on("l.traveler_id AS subject_id", [{"subject_id": CLIENT, **tag_row(applies_to="client")}])
    assert (await service.of_many(tenant_ctx(conn), "client", [CLIENT]))[CLIENT][0]["code"] == "vip"
    assert await service.of_many(tenant_ctx(conn), "trip", []) == {}


def bulk_row(n: int, **over: Any) -> dict[str, Any]:
    base = {"id": uuid.UUID(int=n), "archived": False, "has_tag": False, "held": False}
    base.update(over)
    return base


async def test_bulk_previews_the_count_then_applies_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def on_tag_added(*_a: Any, **kw: Any) -> int:
        return 0 if kw["client_archived"] else 1

    monkeypatch.setattr(triggers, "on_tag_added", on_tag_added)
    rows = [bulk_row(1), bulk_row(2, has_tag=True), bulk_row(3, archived=True)]
    conn = ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row(applies_to="client", code="repeat"))
    conn.on("FROM crm.traveler tr", rows).on("INSERT INTO crm.tag_link (tag_id, traveler_id)", LINK)
    ctx = tenant_ctx(conn, role="agent")
    sel = service.Selection(archived=None, owner="me", tag="newsletter", ids=())
    preview = await service.bulk(ctx, TAG, action="add", selection=sel)
    assert (preview["total"], preview["count"], preview["already"], preview["archived_suppressed"]) == (3, 2, 1, 1)
    assert preview["dry_run"] is True and preview["sample"] == [str(uuid.UUID(int=1)), str(uuid.UUID(int=3))]
    assert conn.sql("FROM crm.traveler tr")[0][3] == MEMBER  # owner "me"
    with pytest.raises(Conflict) as err:
        await service.bulk(ctx, TAG, action="add", selection=sel, dry_run=False, expected=5)
    assert err.value.code == "count_changed" and err.value.extra["count"] == 2
    done = await service.bulk(ctx, TAG, action="add", selection=sel, dry_run=False, expected=2)
    assert done["workflows_started"] == 1 and len(conn.sql("INSERT INTO crm.tag_link")) == 2
    assert ctx.audit_events[-1].action == "tag_bulk_add"
    # Remove skips held records.
    conn = ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row()).on("FROM crm.trip t", [
        bulk_row(1, has_tag=True), bulk_row(2, has_tag=True, held=True), bulk_row(3)])  # fmt: skip
    ctx = tenant_ctx(conn, role="owner")
    sel = service.Selection(stage="proposal")
    preview = await service.bulk(ctx, TAG, action="remove", selection=sel)
    assert (preview["count"], preview["held"], preview["already"]) == (1, 1, 1)
    await service.bulk(ctx, TAG, action="remove", selection=sel, dry_run=False, expected=1)
    assert conn.sql("DELETE FROM crm.tag_link")[0] == (TAG, uuid.UUID(int=1), None)
    conn.on("DELETE FROM crm.tag_link", HoldError("held"))
    with pytest.raises(Conflict):
        await service.bulk(ctx, TAG, action="remove", selection=sel, dry_run=False, expected=1)


async def test_bulk_refusals() -> None:
    conn = ScriptedConn().on("FROM crm.tag WHERE tag_id", tag_row())
    with pytest.raises(Forbidden):
        await service.bulk(
            tenant_ctx(conn, role="assistant"), TAG, action="add", selection=service.Selection(ids=(TRIP,))
        )
    ctx = tenant_ctx(conn)
    bad = [
        ("tag", service.Selection(ids=(TRIP,)), "invalid_action"),
        ("add", service.Selection(), "selection_required"),
        ("add", service.Selection(stage="nowhere"), "invalid_filter"),
        ("add", service.Selection(archived=True), "invalid_filter"),
        ("add", service.Selection(tag="Bad Tag"), "invalid_filter"),
        ("add", service.Selection(owner="someone"), "invalid_filter"),
        (
            "add",
            service.Selection(ids=tuple(uuid.UUID(int=n) for n in range(service.MAX_BULK + 1))),
            "too_many_records",
        ),
    ]
    for action, sel, code in bad:
        with pytest.raises(Invalid) as err:
            await service.bulk(ctx, TAG, action=action, selection=sel)
        assert err.value.code == code, code
    conn.on("FROM crm.trip t", [bulk_row(n) for n in range(service.MAX_BULK + 1)])
    with pytest.raises(Invalid):
        await service.bulk(ctx, TAG, action="add", selection=service.Selection(owner=str(MEMBER)))
    conn.on("FROM crm.tag WHERE tag_id", tag_row(kind="system"))
    with pytest.raises(Conflict):
        await service.bulk(ctx, TAG, action="add", selection=service.Selection(ids=(TRIP,)))
    conn.on("FROM crm.tag WHERE tag_id", tag_row(archived_at=dt.datetime.now(dt.UTC)))
    with pytest.raises(Conflict):
        await service.bulk(ctx, TAG, action="add", selection=service.Selection(ids=(TRIP,)))


async def test_ready_to_travel_list_explains_itself() -> None:
    name = enc("traveler", "display_name_enc", "Mira Solen")
    row = {"traveler_id": CLIENT, "display_name_enc": name, "owner_member_id": MEMBER,
           "reasons": json.dumps([{"code": "returned_months_ago", "months": 14}]),
           "since": dt.datetime(2026, 11, 1, tzinfo=dt.UTC), "marketing": None, "last_trip_id": TRIP,
           "last_trip_title": "Northern hills", "last_trip_start": dt.date(2025, 9, 1),
           "last_trip_end": dt.date(2025, 9, 9)}  # fmt: skip
    conn = ScriptedConn().on("SELECT count(*)::int", 51).on("SELECT tr.traveler_id", [row])
    ctx = tenant_ctx(conn, role="agent")
    got = await ready.listing(ctx, FakeVault(), owner="me", page=1)
    item = got["items"][0]
    assert item["reasons"] == [{"code": "returned_months_ago", "months": 14}] and item["marketing_consent"] == "unknown"
    assert item["is_mine"] and item["last_trip"]["title"] == "Northern hills" and got["has_more"] is True
    assert ctx.audit_events[-1].reason == "ready_to_travel"
    assert ready._consent(True) == "yes" and ready._consent(False) == "no"
    conn.on("SELECT tr.traveler_id", [{**row, "last_trip_id": None, "owner_member_id": None, "reasons": None}])
    assert (await ready.listing(ctx, FakeVault()))["items"][0]["last_trip"] is None
    for kw in ({"page": 0}, {"owner": "them"}):
        with pytest.raises(Invalid):
            await ready.listing(ctx, FakeVault(), **kw)  # type: ignore[arg-type]
    with pytest.raises(Forbidden):
        await ready.listing(tenant_ctx(conn, role="assistant"), FakeVault())


async def test_system_refresh_adds_updates_and_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    fired: list[tuple[Any, ...]] = []

    async def on_tag_added(ctx: Any, applies_to: str, code: str, subject: Any, link: Any, **_: Any) -> int:
        fired.append((applies_to, code, subject))
        return 1

    monkeypatch.setattr(triggers, "on_tag_added", on_tag_added)
    trek, multi, ready_tag = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stale_trip = uuid.uuid4()
    conn = ScriptedConn()
    conn.on("SELECT tag_id, applies_to, code FROM crm.tag WHERE kind = 'system'", [
        {"tag_id": trek, "applies_to": "trip", "code": "sys_aa_trekking"},
        {"tag_id": multi, "applies_to": "trip", "code": "sys_aa_multi_day"},
        {"tag_id": ready_tag, "applies_to": "client", "code": model.READY_TAG}])  # fmt: skip
    conn.on(
        "c.body -> 'facets' AS facets",
        [{"trip_id": TRIP, "source_id": "T-1", "duration_days": 3, "facets": '{"activities": ["trekking"]}'}],
    )
    links = {
        "trip": [
            {"tag_link_id": uuid.uuid4(), "tag_id": multi, "trip_id": TRIP, "traveler_id": None,
             "reasons": json.dumps([{"code": "aa_tour", "tour": "T-OLD"}]), "code": "sys_aa_multi_day"},
            {"tag_link_id": uuid.uuid4(), "tag_id": trek, "trip_id": stale_trip, "traveler_id": None, "reasons": "[]",
             "code": "sys_aa_trekking"},
        ],
        "client": [],
    }  # fmt: skip
    conn.on("WHERE l.source = 'system' AND g.applies_to", lambda applies: links[applies])
    conn.on("INSERT INTO crm.tag_link (tag_id, trip_id, source", LINK)
    counts = await system.refresh(system_ctx(conn), dt.date(2026, 11, 12), clients=False)
    assert counts["system_tags_added"] == 1 and counts["system_tags_updated"] == 1 and counts["tag_runs_started"] == 1
    assert fired == [("trip", "sys_aa_trekking", TRIP)]
    assert conn.sql("DELETE FROM crm.tag_link WHERE tag_link_id = ANY")[0][0] == [links["trip"][1]["tag_link_id"]]
    assert counts["system_tags_held"] == 1  # the fake reports no deleted row: counted as held
    ensure = conn.sql("INSERT INTO crm.tag (kind, applies_to, code, name, tone)")[0]
    assert set(ensure[1]) == set(model.SYSTEM_TAGS) and ensure[3] == model.SYSTEM_TONE


async def test_system_refresh_marks_ready_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    async def on_tag_added(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(triggers, "on_tag_added", on_tag_added)
    ready_tag = uuid.uuid4()
    other = uuid.uuid4()
    conn = ScriptedConn()
    conn.on("SELECT tag_id, applies_to, code FROM crm.tag WHERE kind = 'system'",
            [{"tag_id": ready_tag, "applies_to": "client", "code": model.READY_TAG}])  # fmt: skip
    conn.on("JOIN crm.trip_traveler tt ON tt.traveler_id = tr.traveler_id", [
        {"traveler_id": CLIENT, "archived": False, "trip_id": TRIP, "stage": "completed",
         "start_date": dt.date(2025, 1, 5), "end_date": dt.date(2025, 1, 15), "destinations": ["vn"]},
        {"traveler_id": other, "archived": False, "trip_id": uuid.uuid4(), "stage": "completed",
         "start_date": dt.date(2026, 9, 1), "end_date": dt.date(2026, 9, 9), "destinations": []},
    ])  # fmt: skip
    conn.on("LEFT JOIN crm.aa_catalog_tour c", [{"trip_id": TRIP, "tour": "T-1", "country_code": "VN"}])
    conn.on("GROUP BY country_code", [{"country_code": "VN", "tours": ["T-1", "T-5"]}])
    conn.on("INSERT INTO crm.tag_link (tag_id, traveler_id, source", LINK)
    counts = await system.refresh(system_ctx(conn), dt.date(2026, 11, 12), clients=True)
    assert counts["system_tags_added"] == 1
    args = conn.sql("INSERT INTO crm.tag_link (tag_id, traveler_id, source")[0]
    assert args[1] == CLIENT
    assert json.loads(args[2]) == [{"code": "returned_months_ago", "months": 21},
                                   {"code": "anniversary_soon", "days": 54},
                                   {"code": "new_aa_tours", "country": "VN", "count": 1}]  # fmt: skip


async def test_coverage_lists_trips_in_state_without_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_workflows_model import facts

    other = uuid.UUID(int=9)
    covered = uuid.UUID(int=10)
    by_id = {
        TRIP: facts(trip_id=TRIP, stage="booked", tags=("vip",)),
        other: facts(trip_id=other, stage="booked", deleted=True),
        covered: facts(trip_id=covered, stage="booked"),
    }

    async def trips(_ctx: Any, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
        return {i: by_id[i] for i in ids if i in by_id}

    monkeypatch.setattr(coverage.wf_facts, "trips", trips)
    tag_rule = uuid.UUID(int=11)
    conn = ScriptedConn().on("FROM crm.workflow_rule", [
        {"workflow_rule_id": RULE, "name": "Booked checklist", "template_key": None, "trigger_kind": "stage_enter",
         "trigger_value": "booked", "conditions": "{}"},
        {"workflow_rule_id": tag_rule, "name": "VIP welcome", "template_key": None, "trigger_kind": "event",
         "trigger_value": "trip_tag.added", "conditions": {"tags_any": ["vip"]}},
    ])  # fmt: skip
    conn.on("SELECT t.trip_id FROM crm.trip t", [{"trip_id": i} for i in (TRIP, other, covered, uuid.UUID(int=99))])
    conn.on("FROM crm.workflow_run", [{"workflow_rule_id": RULE, "trip_id": covered}])
    got = await coverage.coverage(tenant_ctx(conn))
    assert [(r["name"], r["missing_count"], r["stage"]) for r in got["rules"]] == [
        ("Booked checklist", 1, "booked"), ("VIP welcome", 1, None)]  # fmt: skip
    assert got["by_trip"] == {str(TRIP): [str(RULE), str(tag_rule)]}
    assert await coverage.coverage(tenant_ctx(ScriptedConn())) == {"rules": [], "by_trip": {}}
