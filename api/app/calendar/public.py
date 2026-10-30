"""``GET /crm/public/v1/calendar/{token}.ics`` (AA-961): a member's calendar feed, fetched by their calendar app.

No sign-in: the 43-character token in the URL is the credential (only its SHA-256 is stored). The feed opens through
:func:`app.tenancy.calendar_feed_tx` (unknown, revoked, expired token, an inactive member or a locked agency: 404
``feed_not_found``). The window is :data:`PAST_DAYS` back to :data:`AHEAD_DAYS` ahead of today in the agency's
timezone; scope ``mine`` reads the member's own trips, tasks and calls. The fetch counters are written at most once per
:data:`TOUCH_EVERY` (calendar apps poll often). The body is :func:`app.calendar.ics.render`: no names, no titles, no
money.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable

import asyncpg

from app.calendar import events, ics
from app.tenancy import calendar_feed_tx

PAST_DAYS = 30
AHEAD_DAYS = 365
TOUCH_EVERY = dt.timedelta(minutes=10)
SETTINGS_SQL = """
SELECT coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1) AS tz,
       (SELECT display_name FROM crm.tenant LIMIT 1) AS agency
"""
TOUCH_SQL = """
UPDATE crm.calendar_feed SET last_fetched_at = now(), fetch_count = least(fetch_count + 1, 2147483647)
 WHERE calendar_feed_id = $1
"""


async def feed(
    pool: asyncpg.Pool,
    token: str,
    *,
    web_origin: str,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> str:
    """The ICS text of the feed ``token`` opens."""
    at = now()
    async with calendar_feed_tx(pool, hashlib.sha256(token.encode()).hexdigest()) as (ctx, found):
        row = await ctx.conn.fetchrow(SETTINGS_SQL, events.DEFAULT_TZ)
        tz = events.zone(row["tz"])
        today = at.astimezone(tz).date()
        items = await events.events(
            ctx,
            start=today - dt.timedelta(days=PAST_DAYS),
            end=today + dt.timedelta(days=AHEAD_DAYS),
            member_id=found.member_id if found.scope == "mine" else None,
            tz=tz,
        )
        if found.last_fetched_at is None or at - found.last_fetched_at >= TOUCH_EVERY:
            await ctx.conn.execute(TOUCH_SQL, found.calendar_feed_id)
    name = f"{row['agency'] or 'Agency'} CRM" + (" (my trips)" if found.scope == "mine" else "")
    return ics.render(items, calendar_name=name, web_origin=web_origin, now=at)
