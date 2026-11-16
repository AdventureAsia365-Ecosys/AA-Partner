"""Who a campaign goes to, when, and whether its content may go (AA-968, card CRM-36). Every function takes an open
context (a member's ``tenant_tx`` or the send job's ``system_tx``) and never opens one.

- :func:`audience`: the agency's live, not archived clients (with the campaign's client tags when it has some), each
  classified: ``no_email`` (no address), ``no_consent`` (the latest ``crm.consent`` row of purpose ``marketing`` is
  not a yes: card R "contacts without consent are excluded"), ``suppressed`` (the address is on the agency's
  ``crm.campaign_suppression`` list: card R "an unsubscribed contact re-imported stays suppressed"), else eligible.
- :func:`gate`: :func:`app.campaigns.model.content_gate` with the campaign's sealed text opened and the catalogue
  cache's state of its AA tours and AA block.
- :func:`plan`: the eligible clients of a campaign spread over days from its start day (card E) and stored as
  ``crm.campaign_recipient`` rows ``planned``; :func:`day_room` is what one mailbox may still send on a day.

Nothing here logs a name, an address or the campaign's text.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app import platform_settings as ps
from app.campaigns import model
from app.tenancy import AnyContext
from app.vault import Vault

MAIL_PURPOSE = "mail"
DEFAULT_TZ = "Asia/Ho_Chi_Minh"

TODAY_SQL = "SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1))::date"
LOCAL_DAY_SQL = (
    "SELECT ($1::timestamptz AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $2))::date"
)
AUDIENCE_SQL = """
SELECT t.traveler_id, t.email_bidx, t.email_enc IS NOT NULL AS has_email,
       (SELECT c.value FROM crm.consent c WHERE c.traveler_id = t.traveler_id AND c.purpose = 'marketing'
         ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC LIMIT 1) AS marketing,
       EXISTS (SELECT 1 FROM crm.campaign_suppression s
                WHERE s.email_bidx = t.email_bidx AND s.deleted_at IS NULL) AS suppressed
  FROM crm.traveler t
 WHERE t.deleted_at IS NULL AND t.archived_at IS NULL
   AND (cardinality($1::text[]) = 0 OR EXISTS (
        SELECT 1 FROM crm.tag_link l JOIN crm.tag g ON g.tag_id = l.tag_id
         WHERE l.traveler_id = t.traveler_id AND g.applies_to = 'client' AND g.code = ANY($1::text[])))
 ORDER BY t.traveler_id
"""
TOURS_SQL = """
SELECT source_id, served_state FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = ANY($1::text[])
"""
CONTENT_SQL = "SELECT listed FROM crm.aa_content WHERE content_id = $1 AND kind = 'campaign_block'"
# The room of one mailbox on given days: the recipients the agency's campaigns planned or queued on it those days
# (queued counted on the day they were queued, in the agency's timezone).
USED_SQL = """
SELECT d AS day, count(*)::int AS n FROM (
    SELECT CASE WHEN r.state = 'queued'
                THEN (r.queued_at AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $3))::date
                ELSE r.send_on END AS d
      FROM crm.campaign_recipient r
      JOIN crm.campaign c ON c.campaign_id = r.campaign_id
     WHERE c.channel_account_id = $1 AND r.state IN ('planned', 'queued') AND r.deleted_at IS NULL
       AND ($4::uuid IS NULL OR r.campaign_id <> $4)
  ) x
 WHERE d >= $2
 GROUP BY d
"""
INSERT_SQL = """
INSERT INTO crm.campaign_recipient (campaign_id, traveler_id, send_on)
SELECT $1, x.traveler_id, x.send_on FROM unnest($2::uuid[], $3::date[]) AS x (traveler_id, send_on)
ON CONFLICT (tenant_id, campaign_id, traveler_id) DO NOTHING
"""
CLEAR_PLAN_SQL = "DELETE FROM crm.campaign_recipient WHERE campaign_id = $1 AND state = 'planned'"
DONE_SQL = "SELECT array_agg(traveler_id) FROM crm.campaign_recipient WHERE campaign_id = $1 AND state <> 'planned'"


@dataclass(frozen=True)
class Links:
    """Where a campaign's links may point (d21): the client host's origin (its AA tour pages and the unsubscribe
    page) and the TripPlanner site's origin. Either may be empty (not configured)."""

    client_origin: str | None
    tripplanner_origin: str | None


def links_from(settings: Any) -> Links:
    """The process settings' campaign links: the client host is the first ``INTAKE_PAGE_ORIGINS`` entry (like the trip
    page's AA tour link), the planner is ``TRIPPLANNER_SITE_ORIGIN``."""
    origins = getattr(settings, "intake_page_origins", ()) or ()
    planner = getattr(settings, "tripplanner_site_origin", "") or None
    return Links(client_origin=origins[0].rstrip("/") if origins else None, tripplanner_origin=planner)


@dataclass(frozen=True)
class Audience:
    eligible: list[uuid.UUID]
    excluded: dict[str, int]
    total: int


async def today(ctx: AnyContext) -> dt.date:
    return await ctx.conn.fetchval(TODAY_SQL, DEFAULT_TZ)


async def local_day(ctx: AnyContext, at: dt.datetime) -> dt.date:
    return await ctx.conn.fetchval(LOCAL_DAY_SQL, at, DEFAULT_TZ)


async def audience(ctx: AnyContext, tags: list[str], *, skip: set[uuid.UUID] | None = None) -> Audience:
    """The campaign's clients, eligible ones in id order. ``skip``: clients already queued or skipped (a re-plan)."""
    excluded: Counter[str] = Counter()
    eligible: list[uuid.UUID] = []
    rows = await ctx.conn.fetch(AUDIENCE_SQL, tags)
    for r in rows:
        if skip and r["traveler_id"] in skip:
            continue
        code = exclusion(r)
        if code is None:
            eligible.append(r["traveler_id"])
        else:
            excluded[code] += 1
    return Audience(eligible, dict(sorted(excluded.items())), len(rows))


def exclusion(row: Any) -> str | None:
    """Why a client gets no campaign mail, or None (in rule order: no address, no consent, suppressed)."""
    if not row["has_email"] or row["email_bidx"] is None:
        return "no_email"
    if row["marketing"] is not True:
        return "no_consent"
    if row["suppressed"]:
        return "suppressed"
    return None


async def texts(vault: Vault, campaign: Any) -> tuple[str, str]:
    """The campaign's subject and text, opened (empty when not written yet)."""
    cid = campaign["campaign_id"]
    subject = (
        await vault.decrypt("campaign", "subject_enc", cid, campaign["subject_enc"], purpose=MAIL_PURPOSE)
        if campaign["subject_enc"]
        else ""
    )
    body = (
        await vault.decrypt("campaign", "body_enc", cid, campaign["body_enc"], purpose=MAIL_PURPOSE)
        if campaign["body_enc"]
        else ""
    )
    return subject, body


async def gate(ctx: AnyContext, campaign: Any, subject: str, body: str, links: Links) -> list[str]:
    """The d21 codes of the campaign as it stands (empty = it may go)."""
    tours = list(campaign["aa_tour_ids"] or [])
    states = {r["source_id"]: r["served_state"] for r in await ctx.conn.fetch(TOURS_SQL, tours)} if tours else {}
    listed: bool | None = None
    if campaign["aa_content_id"] is not None:
        listed = await ctx.conn.fetchval(CONTENT_SQL, campaign["aa_content_id"]) is True
    return model.content_gate(
        subject=subject,
        body=body,
        aa_tour_ids=tours,
        tour_states=states,
        content_listed=listed,
        brand_only=bool(campaign["brand_only"]),
        client_origin=links.client_origin,
        tripplanner_origin=links.tripplanner_origin,
    )


async def daily_limit(ctx: AnyContext) -> int:
    """The mailbox daily send limit in force for this agency (its own value, else the platform's)."""
    return int(await ps.tenant_get(ctx.conn, ps.CAMPAIGN_MAILBOX_DAILY_LIMIT, ctx.tenant_id))


async def max_recipients(ctx: AnyContext) -> int:
    return int(await ps.tenant_get(ctx.conn, ps.CAMPAIGN_MAX_RECIPIENTS, ctx.tenant_id))


async def used(
    ctx: AnyContext, mailbox: uuid.UUID, start: dt.date, *, other_than: uuid.UUID | None = None
) -> dict[dt.date, int]:
    """What the agency's campaigns already planned or queued on ``mailbox`` from ``start`` on, per day."""
    rows = await ctx.conn.fetch(USED_SQL, mailbox, start, DEFAULT_TZ, other_than)
    return {r["day"]: int(r["n"]) for r in rows}


async def day_room(ctx: AnyContext, mailbox: uuid.UUID, day: dt.date, limit: int) -> int:
    """How many more campaign mails ``mailbox`` may queue on ``day``: the limit minus the campaign mails that leave
    it that day (queued that day; queued earlier and still waiting, in the queue or held for a disconnected mailbox
    that releases them on reconnect; queued earlier and sent that day). So a mailbox that was disconnected never sends
    more than its limit in a day after it reconnects. Planned ones do not count: a planned row of another campaign is
    a reservation the queue step honours by order, see :mod:`.send`."""
    leaving = await ctx.conn.fetchval(QUEUED_ON_SQL, mailbox, day, DEFAULT_TZ)
    return max(limit - int(leaving or 0), 0)


QUEUED_ON_SQL = """
SELECT count(*)::int
  FROM crm.campaign_recipient r
  JOIN crm.campaign c ON c.campaign_id = r.campaign_id
  LEFT JOIN crm.dispatch_queue d ON d.dispatch_id = r.dispatch_id
 CROSS JOIN LATERAL (SELECT coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $3) AS tz) z
 WHERE c.channel_account_id = $1 AND r.state = 'queued' AND r.deleted_at IS NULL
   AND ((r.queued_at AT TIME ZONE z.tz)::date = $2
        OR d.status = 'queued'
        OR (d.status = 'held' AND d.gate_result IN ('held_reconnect', 'no_sender'))
        OR (d.status = 'sent' AND (d.decided_at AT TIME ZONE z.tz)::date = $2))
"""


async def plan(
    ctx: AnyContext, campaign_id: uuid.UUID, mailbox: uuid.UUID, eligible: list[uuid.UUID], start: dt.date, limit: int
) -> dict[str, Any]:
    """Replace the campaign's planned rows with ``eligible`` spread from ``start`` (rows already queued or skipped are
    kept and left out of ``eligible`` by the caller). Returns the first and last day and the number of days."""
    await ctx.conn.execute(CLEAR_PLAN_SQL, campaign_id)
    taken = await used(ctx, mailbox, start, other_than=campaign_id)
    days = model.spread(len(eligible), start, limit, taken)
    if eligible:
        await ctx.conn.execute(INSERT_SQL, campaign_id, eligible, days)
    return {
        "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
        "days": len(set(days)),
    }


async def done_clients(ctx: AnyContext, campaign_id: uuid.UUID) -> set[uuid.UUID]:
    """The clients of a campaign already queued or skipped (a re-plan leaves them out)."""
    found = await ctx.conn.fetchval(DONE_SQL, campaign_id)
    return set(found or [])


def excluded_json(excluded: dict[str, int]) -> str:
    return json.dumps(excluded, sort_keys=True)
