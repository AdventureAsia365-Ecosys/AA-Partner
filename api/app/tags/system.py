"""The system-tag refresh (AA-960, card CRM-29b; R29, R36): run by the hourly workflow tick of each agency
(:func:`app.workflows.tick.run_agency`, actor ``job:crm_workflow_tick``: crm-061 lets only a job write system tags).

1. The agency's settings row (crm.tag hangs on it) and system tag rows exist (:data:`app.tags.model.SYSTEM_TAGS`, one
   INSERT ... ON CONFLICT DO NOTHING each).
2. **Trip tags** (R29), every run: the live AA tours of every live trip (its AA booking lines that are not cancelled,
   and the AA options of its live proposals) joined with the AA catalogue cache (``crm.aa_catalog_tour``, served
   tours only: root ADR 0015) give the codes of :func:`app.tags.model.trip_system_tags` with their reasons.
3. **Ready to travel again** (R36), when ``clients`` is set (the tick's special-date hours, once or twice a day): every
   live client with a completed trip is checked with :func:`app.tags.model.ready_to_travel`.

The links are made to match: a new link (source ``system``, its reasons) starts the tag workflows
(:func:`app.workflows.triggers.on_tag_added`); a link whose reasons changed is updated; a link no longer true is
deleted, except on a held trip or client (hold rule 4h: it stays until the hold ends). Ids, codes and counts only.

A system link comes and goes with facts the agency does not control (a CIS tour paused then served again, a trip
trashed and restored, an AA line cancelled and re-added), so its runs are not keyed by the link (a new link each time)
but by :func:`start_key`: a trip tag starts a rule once per trip and code, ever; "ready to travel again" once per
client and return home (the end date of their last completed trip): after another trip it may start the rule again.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import Counter
from typing import Any

from app.tags import model
from app.tenancy import AnyContext

MAX_TRIPS = 5000
MAX_CLIENTS = 5000

ENSURE_SQL = """
INSERT INTO crm.tag (kind, applies_to, code, name, tone)
SELECT 'system', s.applies_to, s.code, s.name, $4
  FROM unnest($1::text[], $2::text[], $3::text[]) AS s(applies_to, code, name)
ON CONFLICT (tenant_id, applies_to, code) DO NOTHING
"""
# crm.tag hangs on the agency's settings row (crm-061 tag_settings_fk); provisioning does not make that row.
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
SYSTEM_IDS_SQL = "SELECT tag_id, applies_to, code FROM crm.tag WHERE kind = 'system'"
TRIP_TOURS_SQL = """
SELECT x.trip_id, c.source_id, c.duration_days, c.body -> 'facets' AS facets
  FROM (SELECT l.trip_id, l.aa_tour_source_id AS tour
          FROM crm.booking_line l JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
         WHERE l.supplier_kind = 'aa' AND l.status <> 'cancelled' AND l.deleted_at IS NULL
           AND l.aa_tour_source_id IS NOT NULL
        UNION
        SELECT o.trip_id, o.aa_tour_source_id
          FROM crm.option o
          JOIN crm.proposal p ON p.proposal_id = o.proposal_id AND p.deleted_at IS NULL
          JOIN crm.trip t ON t.trip_id = o.trip_id AND t.deleted_at IS NULL
         WHERE o.source = 'aa' AND o.deleted_at IS NULL AND o.aa_tour_source_id IS NOT NULL) x
  JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = x.tour AND c.served_state = 'served'
 ORDER BY x.trip_id, c.source_id
 LIMIT $1
"""
LINKS_SQL = """
SELECT l.tag_link_id, l.tag_id, l.trip_id, l.traveler_id, l.reasons, g.code
  FROM crm.tag_link l JOIN crm.tag g ON g.tag_id = l.tag_id
 WHERE l.source = 'system' AND g.applies_to = $1
"""
ADD_TRIP_SQL = """
INSERT INTO crm.tag_link (tag_id, trip_id, source, reasons) VALUES ($1, $2, 'system', $3::jsonb)
ON CONFLICT (tenant_id, tag_id, trip_id) WHERE trip_id IS NOT NULL DO NOTHING
RETURNING tag_link_id
"""
ADD_CLIENT_SQL = """
INSERT INTO crm.tag_link (tag_id, traveler_id, source, reasons) VALUES ($1, $2, 'system', $3::jsonb)
ON CONFLICT (tenant_id, tag_id, traveler_id) WHERE traveler_id IS NOT NULL DO NOTHING
RETURNING tag_link_id
"""
REASONS_SQL = "UPDATE crm.tag_link SET reasons = $2::jsonb WHERE tag_link_id = $1"
DROP_SQL = """
DELETE FROM crm.tag_link WHERE tag_link_id = ANY($1::uuid[]) AND NOT crm.hold_row(trip_id, traveler_id)
"""
CLIENT_TRIPS_SQL = """
SELECT tr.traveler_id, tr.archived_at IS NOT NULL AS archived, t.trip_id, t.stage, t.start_date, t.end_date,
       t.destinations
  FROM crm.traveler tr
  JOIN crm.trip_traveler tt ON tt.traveler_id = tr.traveler_id AND tt.deleted_at IS NULL
  JOIN crm.trip t ON t.trip_id = tt.trip_id AND t.deleted_at IS NULL
 WHERE tr.deleted_at IS NULL
   AND tr.traveler_id IN (
        SELECT tt2.traveler_id FROM crm.trip_traveler tt2
          JOIN crm.trip t2 ON t2.trip_id = tt2.trip_id AND t2.deleted_at IS NULL AND t2.stage = 'completed'
         WHERE tt2.deleted_at IS NULL
         ORDER BY tt2.traveler_id
         LIMIT $1)
 ORDER BY tr.traveler_id, t.trip_id
"""
TRIP_AA_TOURS_SQL = """
SELECT l.trip_id, l.aa_tour_source_id AS tour, c.country_code
  FROM crm.booking_line l
  LEFT JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = l.aa_tour_source_id
 WHERE l.trip_id = ANY($1::uuid[]) AND l.supplier_kind = 'aa' AND l.status <> 'cancelled' AND l.deleted_at IS NULL
   AND l.aa_tour_source_id IS NOT NULL
"""
SERVED_SQL = """
SELECT country_code, array_agg(source_id ORDER BY source_id) AS tours
  FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND served_state = 'served' AND country_code = ANY($1::text[])
 GROUP BY country_code
"""


def start_key(applies_to: str, subject: uuid.UUID, code: str, episode: dt.date | None = None) -> str:
    """The workflow trigger key of a system tag on a subject (crm.workflow_run ``trigger_key``): the same tag on the
    same trip (or the same return home of a client) never starts a rule twice, however often the link is remade."""
    key = f"tag:sys:{applies_to}:{subject}:{code}"
    return f"{key}:{episode.isoformat()}" if episode is not None else key


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


def _dump(reasons: list[dict[str, Any]]) -> str:
    return json.dumps(reasons, separators=(",", ":"), sort_keys=True)


async def ensure_tags(ctx: AnyContext) -> dict[tuple[str, str], uuid.UUID]:
    """The agency's system tag rows, made when missing: (applies_to, code) -> tag id."""
    items = list(model.SYSTEM_TAGS.items())
    await ctx.conn.execute(ENSURE_SETTINGS_SQL)
    await ctx.conn.execute(
        ENSURE_SQL,
        [applies for _code, (applies, _name) in items],
        [code for code, _v in items],
        [name for _code, (_applies, name) in items],
        model.SYSTEM_TONE,
    )
    return {(r["applies_to"], r["code"]): r["tag_id"] for r in await ctx.conn.fetch(SYSTEM_IDS_SQL)}


async def _sync(
    ctx: AnyContext,
    applies_to: str,
    tag_ids: dict[tuple[str, str], uuid.UUID],
    wanted: dict[tuple[uuid.UUID, str], list[dict[str, Any]]],
    *,
    scope: set[str],
    episodes: dict[uuid.UUID, dt.date] | None = None,
) -> Counter[str]:
    """Make the system links of ``applies_to`` whose code is in ``scope`` match ``wanted`` ((subject, code) ->
    reasons). ``episodes``: the subject's episode in :func:`start_key` (a client's last return home)."""
    from app.workflows import triggers  # app.workflows imports app.tags: import late

    counts: Counter[str] = Counter()
    current: dict[tuple[uuid.UUID, str], Any] = {}
    for r in await ctx.conn.fetch(LINKS_SQL, applies_to):
        if r["code"] in scope:
            current[(r["trip_id"] or r["traveler_id"], r["code"])] = r
    stale = [r["tag_link_id"] for key, r in current.items() if key not in wanted]
    if stale:
        tail = str(await ctx.conn.execute(DROP_SQL, stale)).split()[-1]
        removed = int(tail) if tail.isdigit() else 0
        counts["system_tags_removed"] += removed
        counts["system_tags_held"] += len(stale) - removed
    add_sql = ADD_TRIP_SQL if applies_to == "trip" else ADD_CLIENT_SQL
    for (subject, code), reasons in wanted.items():
        tag_id = tag_ids.get((applies_to, code))
        if tag_id is None:
            continue
        have = current.get((subject, code))
        if have is not None:
            if _dump(_json(have["reasons"]) or []) != _dump(reasons):
                await ctx.conn.execute(REASONS_SQL, have["tag_link_id"], _dump(reasons))
                counts["system_tags_updated"] += 1
            continue
        link_id = await ctx.conn.fetchval(add_sql, tag_id, subject, _dump(reasons))
        if link_id is None:
            continue
        counts["system_tags_added"] += 1
        # An archived client never gets a system tag (ready_to_travel), so no start here is suppressed.
        key = start_key(applies_to, subject, code, (episodes or {}).get(subject))
        counts["tag_runs_started"] += await triggers.on_tag_added(ctx, applies_to, code, subject, link_id, key=key)
    return counts


async def refresh_trips(ctx: AnyContext, tag_ids: dict[tuple[str, str], uuid.UUID]) -> Counter[str]:
    tours: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in await ctx.conn.fetch(TRIP_TOURS_SQL, MAX_TRIPS * 4):
        tours.setdefault(r["trip_id"], []).append(
            {"source_id": r["source_id"], "duration_days": r["duration_days"], "facets": _json(r["facets"])}
        )
    wanted: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}
    for trip_id, items in list(tours.items())[:MAX_TRIPS]:
        for code, reasons in model.trip_system_tags(items).items():
            wanted[(trip_id, code)] = reasons
    return await _sync(ctx, "trip", tag_ids, wanted, scope=set(model.TRIP_SYSTEM_TAGS))


async def histories(ctx: AnyContext) -> dict[uuid.UUID, model.ClientHistory]:
    """Every live client with a completed trip (at most :data:`MAX_CLIENTS`), with their trips' dates and stages, the
    countries and AA tours of those trips, and the live AA tours of those countries."""
    rows = await ctx.conn.fetch(CLIENT_TRIPS_SQL, MAX_CLIENTS)
    trip_ids = list({r["trip_id"] for r in rows})
    tours: dict[uuid.UUID, list[tuple[str, str | None]]] = {}
    for r in await ctx.conn.fetch(TRIP_AA_TOURS_SQL, trip_ids) if trip_ids else []:
        tours.setdefault(r["trip_id"], []).append((r["tour"], r["country_code"]))
    by_client: dict[uuid.UUID, tuple[bool, list[model.PastTrip]]] = {}
    for r in rows:
        trip_tours = tours.get(r["trip_id"], [])
        past = model.PastTrip(
            stage=r["stage"],
            start_date=r["start_date"],
            end_date=r["end_date"],
            countries=model.countries_of(r["destinations"], (c for _t, c in trip_tours if c)),
            aa_tours=tuple(sorted({t for t, _c in trip_tours})),
        )
        by_client.setdefault(r["traveler_id"], (bool(r["archived"]), []))[1].append(past)
    countries = sorted({c for _a, trips in by_client.values() for t in trips for c in t.countries})
    served = {
        r["country_code"].strip(): frozenset(r["tours"] or ())
        for r in (await ctx.conn.fetch(SERVED_SQL, countries) if countries else [])
    }
    return {
        client: model.ClientHistory(archived=archived, trips=tuple(trips), served_by_country=served)
        for client, (archived, trips) in by_client.items()
    }


async def refresh_clients(ctx: AnyContext, tag_ids: dict[tuple[str, str], uuid.UUID], today: dt.date) -> Counter[str]:
    wanted: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}
    episodes: dict[uuid.UUID, dt.date] = {}
    for client, history in (await histories(ctx)).items():
        reasons = model.ready_to_travel(history, today)
        if reasons is not None:
            wanted[(client, model.READY_TAG)] = reasons
            back = model.last_return(history, today)
            if back is not None:
                episodes[client] = back
    return await _sync(ctx, "client", tag_ids, wanted, scope=set(model.CLIENT_SYSTEM_TAGS), episodes=episodes)


async def refresh(ctx: AnyContext, today: dt.date, *, clients: bool) -> Counter[str]:
    """One agency's system tags (the hourly tick). ``clients``: also "ready to travel again" (once or twice a day)."""
    tag_ids = await ensure_tags(ctx)
    counts = await refresh_trips(ctx, tag_ids)
    if clients:
        counts.update(await refresh_clients(ctx, tag_ids, today))
    return counts
