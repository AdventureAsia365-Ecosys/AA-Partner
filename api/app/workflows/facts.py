"""What the workflow engine reads about a trip or a client (AA-959). Every function takes the open context and never
opens one."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.intake.settings import DEFAULT_TIMEZONE
from app.tenancy import AnyContext
from app.workflows.model import TripFacts

TRIP_FACTS_SQL = """
SELECT t.trip_id, t.stage, t.trip_style, t.lead_source, t.destinations,
       cardinality(t.party_child_ages) > 0 AS has_children, t.intake_completeness, t.owner_member_id,
       t.start_date, t.end_date, t.stage_entered_at, t.last_activity_at, t.created_at,
       t.deleted_at IS NOT NULL AS deleted,
       EXISTS (SELECT 1 FROM crm.booking_line l
                WHERE l.trip_id = t.trip_id AND l.supplier_kind = 'aa' AND l.status <> 'cancelled'
                  AND l.deleted_at IS NULL) AS has_aa_lines,
       EXISTS (SELECT 1 FROM crm.trip_cancellation x
                WHERE x.trip_id = t.trip_id AND x.status = 'pending') AS cancelling,
       (SELECT tt.traveler_id FROM crm.trip_traveler tt
         WHERE tt.trip_id = t.trip_id AND tt.role = 'lead' ORDER BY tt.created_at LIMIT 1) AS lead_id
  FROM crm.trip t
 WHERE t.trip_id = ANY($1::uuid[])
"""
TZ_SQL = "SELECT timezone FROM crm.tenant_settings"
CLIENT_SQL = """
SELECT traveler_id, owner_member_id, archived_at IS NOT NULL AS archived, deleted_at IS NOT NULL AS deleted
  FROM crm.traveler WHERE traveler_id = $1
"""
MEMBER_SQL = "SELECT 1 FROM crm.member WHERE member_id = $1 AND status = 'active' AND deleted_at IS NULL"


def _s3(raw: Any) -> int:
    if isinstance(raw, str):
        import json

        raw = json.loads(raw)
    try:
        return int((raw or {}).get("s3") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _facts(r: Any) -> TripFacts:
    return TripFacts(
        trip_id=r["trip_id"],
        stage=r["stage"],
        trip_style=r["trip_style"],
        lead_source=r["lead_source"],
        destinations=tuple(r["destinations"] or ()),
        has_children=bool(r["has_children"]),
        has_aa_lines=bool(r["has_aa_lines"]),
        s3=_s3(r["intake_completeness"]),
        owner_member_id=r["owner_member_id"],
        lead_id=r["lead_id"],
        start_date=r["start_date"],
        end_date=r["end_date"],
        stage_entered_at=r["stage_entered_at"],
        last_activity_at=r["last_activity_at"],
        created_at=r["created_at"],
        deleted=bool(r["deleted"]),
        cancelling=bool(r["cancelling"]),
    )


async def trip(ctx: AnyContext, trip_id: uuid.UUID) -> TripFacts | None:
    rows = await trips(ctx, [trip_id])
    return rows.get(trip_id)


async def trips(ctx: AnyContext, trip_ids: list[uuid.UUID]) -> dict[uuid.UUID, TripFacts]:
    if not trip_ids:
        return {}
    return {r["trip_id"]: _facts(r) for r in await ctx.conn.fetch(TRIP_FACTS_SQL, trip_ids)}


async def timezone(ctx: AnyContext) -> ZoneInfo:
    name = await ctx.conn.fetchval(TZ_SQL) or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


async def client(ctx: AnyContext, traveler_id: uuid.UUID) -> Any:
    return await ctx.conn.fetchrow(CLIENT_SQL, traveler_id)


async def active_member(ctx: AnyContext, member_id: uuid.UUID | None) -> bool:
    return member_id is not None and await ctx.conn.fetchval(MEMBER_SQL, member_id) is not None


def today(tz: ZoneInfo, now: dt.datetime) -> dt.date:
    return now.astimezone(tz).date()
