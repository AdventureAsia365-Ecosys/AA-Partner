"""AA-960 (CRM-29b) in the automations: a rule's tags must be the agency's (422 ``tag_not_found``), the preview of a
tag rule counts the records that carry its tags now, the overview offers the tag events, and the hourly tick refreshes
the system tags in a savepoint (a failure is counted, the tick goes on)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import Counter
from typing import Any

import asyncpg
import pytest

from app.errors import Invalid
from app.workflows import service, tick
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import system_ctx, tenant_ctx
from tests.unit.test_workflows_service import NOW, RULE, TRIP, rule_row, trip_row

pytestmark = pytest.mark.asyncio

CLIENT_RULE = {
    "name": "Welcome back",
    "trigger": {"kind": "event", "value": "client_tag.added"},
    "conditions": {"tags_any": ["sys_ready_to_travel"]},
    "steps": [{"action": "create_task", "kind": "custom", "title": "Call to welcome them back"}],
}


async def test_a_rule_names_tags_of_the_agency() -> None:
    conn = ScriptedConn().on("FROM crm.tag WHERE applies_to = $1", [])
    unknown = {**CLIENT_RULE, "conditions": {"tags_any": ["sys_ready_to_travel", "gold"]}}
    with pytest.raises(Invalid) as err:
        await service.create(tenant_ctx(conn), unknown)
    assert err.value.code == "tag_not_found"
    assert conn.sql("FROM crm.tag WHERE applies_to = $1")[0] == ("client", ["sys_ready_to_travel", "gold"])
    trip_system = {**CLIENT_RULE, "trigger": {"kind": "event", "value": "trip_tag.added"}}
    with pytest.raises(Invalid):  # a client system tag in a trip rule
        await service.create(tenant_ctx(conn), trip_system)
    # A system tag of the right subject is known before the tick has made its row.
    conn.on("INSERT INTO crm.workflow_rule", RULE).on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row(
        trigger_kind="event", trigger_value="client_tag.added",
        conditions=json.dumps(CLIENT_RULE["conditions"])))  # fmt: skip
    out = await service.create(tenant_ctx(conn), CLIENT_RULE)
    assert out["trigger"]["value"] == "client_tag.added"
    trip_rule = {**CLIENT_RULE, "trigger": {"kind": "event", "value": "trip_tag.added"},
                 "conditions": {"tags_any": ["vip"], "stages": ["booked"]}}  # fmt: skip
    await service.create(tenant_ctx(conn.on("FROM crm.tag WHERE applies_to = $1", [{"code": "vip"}])), trip_rule)
    assert conn.sql("FROM crm.tag WHERE applies_to = $1")[-1] == ("trip", ["vip"])


async def test_the_overview_offers_tag_events_and_tags_any() -> None:
    out = await service.overview(tenant_ctx(ScriptedConn()))
    assert out["options"]["tag_events"] == ["trip_tag.added", "client_tag.added"]
    assert "tags_any" in out["options"]["condition_keys"]
    assert {"trip_tag.added", "client_tag.added"} <= set(out["options"]["events"])


async def test_preview_of_tag_rules() -> None:
    tagged = rule_row(trigger_kind="event", trigger_value="client_tag.added",
                      conditions=json.dumps(CLIENT_RULE["conditions"]))  # fmt: skip
    conn = ScriptedConn().on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", tagged)
    conn.on("SELECT DISTINCT l.traveler_id", [{"traveler_id": uuid.UUID(int=5)}])
    out = await service.preview(tenant_ctx(conn), RULE)
    assert out == {"workflow_rule_id": str(RULE), "subject": "client", "count": 1, "sample": [str(uuid.UUID(int=5))]}
    other = uuid.UUID(int=8)
    conn = ScriptedConn().on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row(
        trigger_kind="event", trigger_value="trip_tag.added", conditions='{"tags_any": ["vip"]}'))  # fmt: skip
    conn.on("ORDER BY t.last_activity_at DESC", [{"trip_id": TRIP}, {"trip_id": other}])
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row(tags=["vip"]), trip_row(trip_id=other, tags=[])])
    conn.on("SELECT now()", NOW).on("SELECT timezone", "Asia/Ho_Chi_Minh")
    assert (await service.preview(tenant_ctx(conn), RULE))["sample"] == [str(TRIP)]


async def test_the_tick_refreshes_system_tags_in_a_savepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[dt.date, bool]] = []

    async def refresh(_ctx: Any, today: dt.date, *, clients: bool) -> Counter[str]:
        seen.append((today, clients))
        return Counter({"system_tags_added": 2})

    monkeypatch.setattr(tick.system_tags, "refresh", refresh)
    ctx = system_ctx(ScriptedConn())
    assert await tick._system_tags(ctx, dt.date(2026, 11, 12), clients=True) == Counter({"system_tags_added": 2})
    assert seen == [(dt.date(2026, 11, 12), True)]

    async def broken(*_a: Any, **_k: Any) -> Counter[str]:
        raise asyncpg.PostgresError("boom")

    monkeypatch.setattr(tick.system_tags, "refresh", broken)
    assert await tick._system_tags(ctx, dt.date(2026, 11, 12), clients=False) == Counter({"system_tags_failed": 1})
