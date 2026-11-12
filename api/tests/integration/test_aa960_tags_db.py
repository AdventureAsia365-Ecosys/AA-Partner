"""AA-960 (card CRM-29b) end to end on PostgreSQL with the RDS role shape (crm-061), as crm_app through ``tenant_tx()``
and the tick's ``system_tx()``:

- R3: agency tags on trips and clients, the tag workflows (``trip_tag.added`` / ``client_tag.added``) start once per
  link, bulk apply previews the count and applies it only with that count (card E);
- card R: a tag on an archived client starts no client workflow (audited denied ``client_archived``);
- R29: the tick's refresh puts the AA catalogue facets of a trip's live AA tours on it as system tags, and takes them
  off when the tour is no longer served; members can neither add nor remove a system tag (CRM55 / 409);
- R36: a client home for more than 9 months is "ready to travel again" with its reasons; the list explains them;
- R30: the coverage lists a trip in a rule's stage without a run, and the apply closes the gap;
- RLS: another agency sees none of it.

Invented names, tours and places only.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app import pipeline
from app.errors import Conflict, Forbidden, NotFound
from app.tags import model, ready, service, system
from app.tenancy import system_tx, tenant_tx
from app.travellers import service as travellers
from app.vault import Vault
from app.workflows import coverage
from app.workflows import service as workflows
from tests.integration.test_ops_release_api import DAY, _seed, _setup, _traveller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TICK = "job:crm_workflow_tick"


def _task_rule(name: str, trigger: dict[str, Any], conditions: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "trigger": trigger, "conditions": conditions,
            "steps": [{"action": "create_task", "kind": "custom", "title": f"Follow up: {name}"}]}  # fmt: skip


async def _world(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _setup(pool)
    await _seed(pool, s["tenant"], "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    return s


async def _runs(pool: asyncpg.Pool, tenant: uuid.UUID, rule: str) -> list[Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        return list(await conn.fetch("SELECT trip_id, traveler_id, trigger_key FROM crm.workflow_run "
                                     "WHERE workflow_rule_id = $1 ORDER BY created_at", uuid.UUID(rule)))  # fmt: skip


async def _denied(pool: asyncpg.Pool, tenant: uuid.UUID) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        return int(await conn.fetchval("SELECT count(*) FROM crm.audit_event WHERE action = 'workflow_start' "
                                       "AND outcome = 'denied' AND reason = 'client_archived'"))  # fmt: skip


async def test_agency_tags_bulk_and_tag_workflows(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    t = s["tenant"]
    owner, agent, assistant = (s["who"][r][0] for r in ("owner", "agent", "assistant"))
    async with tenant_tx(app_pool, owner, t) as ctx:
        vip = await service.create(ctx, "trip", "VIP", "purple")
        repeat = await service.create(ctx, "client", "Repeat client")
        on_trip = {"kind": "event", "value": "trip_tag.added"}
        on_client = {"kind": "event", "value": "client_tag.added"}
        trip_rule = await workflows.create(ctx, _task_rule("VIP welcome", on_trip, {"tags_any": [vip["code"]]}))
        client_rule = await workflows.create(
            ctx, _task_rule("Repeat thanks", on_client, {"tags_any": ["repeat_client"]})
        )
    assert (vip["code"], repeat["code"]) == ("vip", "repeat_client")
    # One trip: the tag starts the trip rule once (a second put is a no-op).
    async with tenant_tx(app_pool, assistant, t) as ctx:
        out = await service.put(ctx, uuid.UUID(vip["tag_id"]), trip_id=s["trip"])
        assert out["added"] and out["workflows_started"] == 1
        assert not (await service.put(ctx, uuid.UUID(vip["tag_id"]), trip_id=s["trip"]))["added"]
        tags = await service.of_subject(ctx, trip_id=s["trip"])
        assert [x["code"] for x in tags["tags"]] == ["vip"] and tags["tags"][0]["source"] == "member"
        with pytest.raises(Forbidden):
            await service.bulk(ctx, uuid.UUID(repeat["tag_id"]), action="add",
                               selection=service.Selection(archived=False))  # fmt: skip
    runs = await _runs(app_pool, t, trip_rule["workflow_rule_id"])
    assert len(runs) == 1 and runs[0]["trip_id"] == s["trip"] and runs[0]["trigger_key"].startswith("tag:")
    # Card R: the lead client archived -> tagged, but no client workflow starts (audited denied).
    await _seed(app_pool, t, "UPDATE crm.traveler SET archived_at = now(), archive_reason = 'agent', "
                "archived_by = 'seed:test' WHERE traveler_id = $1", s["lead"])  # fmt: skip
    async with tenant_tx(app_pool, agent, t) as ctx:
        out = await service.put(ctx, uuid.UUID(repeat["tag_id"]), traveler_id=s["lead"])
        assert out["added"] and out["workflows_suppressed"] and out["workflows_started"] == 0
    assert await _runs(app_pool, t, client_rule["workflow_rule_id"]) == []
    assert await _denied(app_pool, t) == 1
    # Card E: bulk add previews the count (the active clients: the child), then applies with that count only.
    tag_id = uuid.UUID(repeat["tag_id"])
    sel = service.Selection(archived=False)
    async with tenant_tx(app_pool, agent, t) as ctx:
        preview = await service.bulk(ctx, tag_id, action="add", selection=sel)
        assert (preview["count"], preview["already"]) == (1, 0) and preview["sample"] == [str(s["child"])]
        with pytest.raises(Conflict):
            await service.bulk(ctx, tag_id, action="add", selection=sel, dry_run=False, expected=2)
    async with tenant_tx(app_pool, agent, t) as ctx:
        done = await service.bulk(ctx, tag_id, action="add", selection=sel, dry_run=False, expected=1)
        assert done["workflows_started"] == 1
        everyone = await service.bulk(
            ctx, tag_id, action="remove", selection=service.Selection(ids=(s["lead"], s["child"]))
        )
        assert everyone["count"] == 2
    runs = await _runs(app_pool, t, client_rule["workflow_rule_id"])
    assert [r["traveler_id"] for r in runs] == [s["child"]]
    # The directory filters by tag and shows the tags; the board card shows the trip's tags.
    async with tenant_tx(app_pool, owner, t) as ctx:
        vault = Vault(s["keys"], ctx)
        listed = await travellers.directory(ctx, vault, tag="repeat_client")
        assert [i["traveler_id"] for i in listed["items"]] == [str(s["child"])]
        assert listed["items"][0]["tags"][0]["code"] == "repeat_client"
        board = await pipeline.board(ctx, vault, pipeline.BoardFilter())
        card = next(c for st in board["stages"] for c in st["cards"] if c["trip_id"] == str(s["trip"]))
        assert [x["code"] for x in card["tags"]] == ["vip"]
        vocab = await service.vocabulary(ctx, applies_to="client")
        assert [(x["code"], x["uses"]) for x in vocab["tags"]] == [("repeat_client", 2)]
        # Removing a tag deletes its links; the archive keeps them.
        await service.update(ctx, tag_id, archived=True)
        with pytest.raises(Conflict):
            await service.put(ctx, tag_id, traveler_id=s["child"])
        assert (await service.delete(ctx, tag_id))["removed_links"] == 2
    # Another agency sees nothing and cannot use this agency's tag.
    other = await _world(app_pool)
    async with tenant_tx(app_pool, other["who"]["owner"][0], other["tenant"]) as ctx:
        assert (await service.vocabulary(ctx))["tags"] == []
        with pytest.raises(NotFound):
            await service.put(ctx, uuid.UUID(vip["tag_id"]), trip_id=other["trip"])


async def test_coverage_lists_a_trip_without_its_stage_rule_and_apply_closes_it(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    t = s["tenant"]
    owner = s["who"]["owner"][0]
    async with tenant_tx(app_pool, owner, t) as ctx:
        rule = await workflows.create(ctx, _task_rule("Booked checklist", {"kind": "stage_enter", "value": "booked"},
                                                      {}))  # fmt: skip
        gaps = await coverage.coverage(ctx)
        [gap] = [g for g in gaps["rules"] if g["workflow_rule_id"] == rule["workflow_rule_id"]]
        assert gap["missing_count"] == 1 and gap["trip_ids"] == [str(s["trip"])] and gap["stage"] == "booked"
        assert gaps["by_trip"][str(s["trip"])] == [rule["workflow_rule_id"]]
        applied = await workflows.apply(ctx, uuid.UUID(rule["workflow_rule_id"]), [s["trip"]], dry_run=False,
                                        request_id=uuid.uuid4())  # fmt: skip
        assert applied["started"] == 1
        assert (await coverage.coverage(ctx))["rules"] == []


async def _tour(pool: asyncpg.Pool, source_id: str, country: str, days: int, facets: dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_system, source_id, title, country_code, duration_days, "
            "served_state, body, checked_at, state_changed_at) VALUES ('cis', $1, $2, $3, $4, 'served', $5::jsonb, "
            "now(), now()) ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served', "
            "body = EXCLUDED.body",
            source_id, f"Invented tour {source_id}", country, days, json.dumps({"facets": facets}),
        )  # fmt: skip


async def _refresh(pool: asyncpg.Pool, tenant: uuid.UUID, today: dt.date, clients: bool = True) -> dict[str, int]:
    async with system_tx(pool, tenant, actor=TICK) as ctx:
        return dict(await system.refresh(ctx, today, clients=clients))


async def test_system_tags_and_ready_to_travel(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    t, today = s["tenant"], s["today"]
    owner, agent = s["who"]["owner"][0], s["who"]["agent"][0]
    tag = uuid.uuid4().hex[:8]
    trek, past, fresh = f"T-TREK-{tag}", f"T-PAST-{tag}", f"T-NEW-{tag}"
    await _tour(app_pool, trek, "QZ", 3, {"activities": ["trekking", "culinary"], "intensity": "challenging"})
    await _tour(app_pool, past, "QX", 2, {"activities": [], "intensity": None})
    await _tour(app_pool, fresh, "QX", 4, {"activities": ["wildlife_nature"], "intensity": "easy"})
    await _seed(app_pool, t, "UPDATE crm.booking_line SET aa_tour_source_id = $2 WHERE booking_line_id = $1",
                s["line"], trek, actor="job:test")  # fmt: skip
    # A returning client: one completed trip that ended 14 months ago, with an AA tour in country QX.
    back = await _traveller(app_pool, s["keys"], t, owner, name="Ilsa Brightwater")
    old = uuid.uuid4()
    start, end = today - 430 * DAY, today - 425 * DAY
    await _seed(app_pool, t, "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, start_date, end_date, "
                "destinations) VALUES ($1, 'Brightwater lakes', 'completed', 'none', $2, $3, ARRAY['qx'])",
                old, start, end)  # fmt: skip
    await _seed(app_pool, t, "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')",
                old, back)  # fmt: skip
    await _seed(app_pool, t, "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, "
                "aa_tour_source_id, service_from, service_to) VALUES ($1, 'aa', crm.ensure_aa_supplier(), "
                "'Lakes loop', $2, $3, $4)", old, past, start, end, actor="job:test")  # fmt: skip
    async with tenant_tx(app_pool, owner, t) as ctx:
        rule = await workflows.create(ctx, _task_rule("Welcome back", {"kind": "event", "value": "client_tag.added"},
                                                      {"tags_any": [model.READY_TAG]}))  # fmt: skip
    counts = await _refresh(app_pool, t, today)
    assert counts["system_tags_added"] >= 4
    async with tenant_tx(app_pool, agent, t) as ctx:
        trip_tags = {x["code"]: x for x in (await service.of_subject(ctx, trip_id=s["trip"]))["tags"]}
        assert {"sys_aa_trekking", "sys_aa_culinary", "sys_aa_challenging", "sys_aa_multi_day"} <= set(trip_tags)
        assert trip_tags["sys_aa_trekking"]["reasons"] == [{"code": "aa_tour", "tour": trek}]
        client_tags = (await service.of_subject(ctx, traveler_id=back))["tags"]
        assert [x["code"] for x in client_tags] == [model.READY_TAG]
        reasons = client_tags[0]["reasons"]
        assert reasons[0] == {"code": "returned_months_ago", "months": model.months_between(end, today)}
        assert any(r["code"] == "new_aa_tours" and r["country"] == "QX" for r in reasons)
        # A member never removes a system tag.
        with pytest.raises(Conflict):
            await service.remove(ctx, uuid.UUID(client_tags[0]["tag_id"]), traveler_id=back)
        with pytest.raises(asyncpg.PostgresError) as err:
            async with ctx.conn.transaction():
                await ctx.conn.execute("DELETE FROM crm.tag_link WHERE traveler_id = $1", back)
        assert err.value.sqlstate == "CRM55"
        listed = await ready.listing(ctx, Vault(s["keys"], ctx))
        [item] = [i for i in listed["items"] if i["traveler_id"] == str(back)]
        assert item["display_name"] == "Ilsa Brightwater" and item["marketing_consent"] == "unknown"
        assert item["last_trip"]["title"] == "Brightwater lakes" and item["reasons"] == reasons
    runs = await _runs(app_pool, t, rule["workflow_rule_id"])
    assert [r["traveler_id"] for r in runs] == [back]
    # Idempotent: the next tick adds and starts nothing.
    again = await _refresh(app_pool, t, today)
    assert again.get("system_tags_added", 0) == 0 and again.get("tag_runs_started", 0) == 0
    # The tour is no longer served: its tags come off the trip; a new open trip takes the client off the list.
    async with app_pool.acquire() as conn:
        await conn.execute("UPDATE crm.aa_catalog_tour SET served_state = 'paused' WHERE source_id = $1", trek)
    await _seed(app_pool, t, "INSERT INTO crm.trip (trip_id, title, stage, waiting_on) VALUES ($1, "
                "'Brightwater return', 'inquiry', 'agency')", new := uuid.uuid4())  # fmt: skip
    await _seed(app_pool, t, "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')",
                new, back)  # fmt: skip
    gone = await _refresh(app_pool, t, today)
    assert gone["system_tags_removed"] >= 5
    async with tenant_tx(app_pool, agent, t) as ctx:
        assert (await service.of_subject(ctx, trip_id=s["trip"]))["tags"] == []
        assert (await ready.listing(ctx, Vault(s["keys"], ctx)))["total"] == 0
