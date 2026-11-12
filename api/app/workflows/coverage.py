"""Workflow coverage (AA-960, card CRM-29b; design v3 R30, WF-1 "N trips missing workflow Y [Preview][Run]", A-02 /
A-17): the trips an active automation should cover by their state today but that never had a run of it.

A rule has a state to check when its trigger is a state the trip is in now:

- ``stage_enter`` X: the live trips in stage X (a rule switched on after they entered it, or a trip moved there before
  a condition held);
- ``event`` ``trip_tag.added``: the live trips that carry one of its tags (a tag put on before the rule existed).

A gap = such a trip that matches the rule's conditions now (:func:`app.workflows.model.matches`), is not lost,
trashed, cancelled or cancelling, and has no ``crm.workflow_run`` of the rule at all (a run that ended or was stopped
counts as covered: stopping is a member's decision). Date, event, special-date and manual rules have no state to
check (their dates and events are past), so they are left out. Computed on read (the design's stored
``workflow_run.coverage_gap`` would need a contract migration and a writer): at most the live trips the preview reads.

[Run] is the existing apply (:func:`app.workflows.service.apply`, dry run first, at most 100 trips per call).
"""

from __future__ import annotations

import json
from typing import Any

from app.tenancy import TenantContext
from app.workflows import facts as wf_facts
from app.workflows import model

SAMPLE = 100

RULES_SQL = """
SELECT workflow_rule_id, name, template_key, trigger_kind, trigger_value, conditions
  FROM crm.workflow_rule
 WHERE status = 'active' AND deleted_at IS NULL
   AND (trigger_kind = 'stage_enter' OR (trigger_kind = 'event' AND trigger_value = 'trip_tag.added'))
 ORDER BY created_at, workflow_rule_id
 LIMIT 200
"""
LIVE_TRIPS_SQL = """
SELECT t.trip_id FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled')
 ORDER BY t.last_activity_at DESC, t.trip_id
 LIMIT 2000
"""
RUNS_SQL = """
SELECT DISTINCT workflow_rule_id, trip_id FROM crm.workflow_run
 WHERE trip_id = ANY($1::uuid[]) AND workflow_rule_id = ANY($2::uuid[])
"""


def _conditions(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


def in_state(rule: Any, conditions: dict[str, Any], facts: model.TripFacts) -> bool:
    """The trip is in the state the rule's trigger names (pure)."""
    if rule["trigger_kind"] == "stage_enter":
        return facts.stage == rule["trigger_value"]
    return bool(set(conditions.get("tags_any") or ()) & set(facts.tags))


async def coverage(ctx: TenantContext) -> dict[str, Any]:
    rules = await ctx.conn.fetch(RULES_SQL)
    out: dict[str, Any] = {"rules": [], "by_trip": {}}
    if not rules:
        return out
    ids = [r["trip_id"] for r in await ctx.conn.fetch(LIVE_TRIPS_SQL)]
    all_facts = await wf_facts.trips(ctx, ids)
    ran = {(r["workflow_rule_id"], r["trip_id"])
           for r in await ctx.conn.fetch(RUNS_SQL, ids, [r["workflow_rule_id"] for r in rules])}  # fmt: skip
    by_trip: dict[str, list[str]] = {}
    for rule in rules:
        conditions = _conditions(rule["conditions"])
        missing: list[str] = []
        for trip_id in ids:
            facts = all_facts.get(trip_id)
            if facts is None or model.stop_reason(facts) is not None:
                continue
            if not in_state(rule, conditions, facts) or model.matches(conditions, facts) is not None:
                continue
            if (rule["workflow_rule_id"], trip_id) in ran:
                continue
            missing.append(str(trip_id))
            by_trip.setdefault(str(trip_id), []).append(str(rule["workflow_rule_id"]))
        if missing:
            out["rules"].append(
                {
                    "workflow_rule_id": str(rule["workflow_rule_id"]),
                    "name": rule["name"],
                    "template_key": rule["template_key"],
                    "trigger": {"kind": rule["trigger_kind"], "value": rule["trigger_value"]},
                    "stage": rule["trigger_value"] if rule["trigger_kind"] == "stage_enter" else None,
                    "missing_count": len(missing),
                    "trip_ids": missing[:SAMPLE],
                }
            )
    out["by_trip"] = by_trip
    return out
