"""Campaigns for the agency's members (AA-968, card CRM-36; design v3 R14, A-15 Marketing). Called by
:mod:`app.routers.campaigns` inside the member's ``tenant_tx``.

Who: everyone with Marketing (owner, agent: ``marketing_view``) reads; owner and agent write and send
(``campaign_send``, design §4.6 "Send campaign"); the assistant does neither. Every write needs the ``campaigns``
feature on the agency's plan (``crm.tenant_entitlement.allowed_features``, projected from the CIS ``crm.campaigns``
entitlement): the feature key is off for every agency until AA allows it after the independent review.

Lifecycle: a **draft** is written freely; **schedule** runs the d21 content gate (any code -> 422 ``content_gate``
with the codes), checks the mailbox (one of the agency's own: the agency mailbox, or the member's own; never AA's
mail), counts the audience (none -> 409 ``audience_empty``; above ``crm.campaign.max_recipients`` -> 409
``audience_over_cap``; the caller sends back the previewed count, another one -> 409 ``count_changed``), and plans the
recipients over days (card E); a **held** campaign (the send pass or the retired-tour pull stopped it) is edited and
scheduled again the same way (the clients whose mail the hold took back are planned anew); schedule also checks
that the mailbox is the agency's or the caller's own (BYO, like the editor); **cancel** skips every planned client
(``cancelled``) and takes back the mails not handed to the provider yet, queued or waiting for a disconnected mailbox
(:func:`app.campaigns.send.withdraw`). The send itself is :mod:`app.campaigns.send`.

Reads give counts per state and per dispatch outcome, never a client name: the campaign report is the agency's own
marketing and Adventure Asia never reads it.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from app import entitlements
from app.campaigns import audience, model, send
from app.errors import Conflict, Invalid, NotFound
from app.permissions import Action, require
from app.tenancy import TenantContext
from app.vault import Vault

FEATURE = "campaigns"
MAIL_PURPOSE = "mail"  # the campaign text and the mailbox address are under the agency's mail DEK
PAGE_SIZE = 50

LIST_SQL = """
SELECT c.campaign_id, c.name, c.state, c.hold_code, c.gate_codes, c.scheduled_at, c.started_at, c.finished_at,
       c.recipient_count, c.excluded, c.daily_limit, c.created_at, c.version,
       (SELECT count(*) FROM crm.campaign_recipient r WHERE r.campaign_id = c.campaign_id
         AND r.state = 'planned')::int AS planned,
       (SELECT count(*) FROM crm.campaign_recipient r WHERE r.campaign_id = c.campaign_id
         AND r.state = 'queued')::int AS queued,
       (SELECT count(*) FROM crm.campaign_recipient r WHERE r.campaign_id = c.campaign_id
         AND r.state = 'skipped')::int AS skipped,
       (SELECT count(*) FROM crm.campaign_recipient r JOIN crm.dispatch_queue d ON d.dispatch_id = r.dispatch_id
         WHERE r.campaign_id = c.campaign_id AND d.status = 'sent')::int AS sent,
       (SELECT count(*) FROM crm.campaign_recipient r WHERE r.campaign_id = c.campaign_id
         AND r.unsubscribed_at IS NOT NULL)::int AS unsubscribed
  FROM crm.campaign c
 WHERE c.deleted_at IS NULL
 ORDER BY c.created_at DESC, c.campaign_id
 LIMIT $1
"""
ONE_SQL = """
SELECT campaign_id, name, locale, subject_enc, body_enc, channel_account_id, aa_tour_ids, aa_content_id, brand_only,
       audience_tags, state, hold_code, gate_codes, scheduled_at, started_at, finished_at, daily_limit,
       recipient_count, excluded, version, created_at, created_by_member_id
  FROM crm.campaign
 WHERE campaign_id = $1 AND deleted_at IS NULL
"""
LOCK_SQL = ONE_SQL + "   FOR UPDATE"
INSERT_SQL = """
INSERT INTO crm.campaign (campaign_id, name, locale, subject_enc, body_enc, channel_account_id, aa_tour_ids,
                          aa_content_id, brand_only, audience_tags, gate_codes, created_by_member_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""
UPDATE_SQL = """
UPDATE crm.campaign
   SET name = $2, locale = $3, subject_enc = $4, body_enc = $5, channel_account_id = $6, aa_tour_ids = $7,
       aa_content_id = $8, brand_only = $9, audience_tags = $10, gate_codes = $11, version = version + 1
 WHERE campaign_id = $1
RETURNING version
"""
SCHEDULE_SQL = """
UPDATE crm.campaign
   SET state = 'scheduled', hold_code = NULL, gate_codes = '{}', scheduled_at = $2, daily_limit = $3,
       recipient_count = $4, excluded = $5::jsonb, version = version + 1
 WHERE campaign_id = $1
RETURNING version
"""
CANCEL_SQL = """
UPDATE crm.campaign SET state = 'cancelled', hold_code = NULL, version = version + 1 WHERE campaign_id = $1
RETURNING version
"""
SKIP_PLANNED_SQL = """
UPDATE crm.campaign_recipient SET state = 'skipped', outcome_code = 'cancelled'
 WHERE campaign_id = $1 AND state = 'planned'
"""
DELETE_SQL = "DELETE FROM crm.campaign WHERE campaign_id = $1 AND state = 'draft'"
MAILBOX_SQL = """
SELECT channel_account_id, member_id FROM crm.channel_account WHERE channel_account_id = $1 AND deleted_at IS NULL
"""
MAILBOXES_SQL = """
SELECT channel_account_id, member_id, kind, status, address_enc FROM crm.channel_account
 WHERE deleted_at IS NULL AND (member_id IS NULL OR member_id = $1)
 ORDER BY member_id NULLS FIRST, channel_account_id
"""
TAGS_SQL = """
SELECT code, name, kind FROM crm.tag WHERE applies_to = 'client' AND archived_at IS NULL ORDER BY kind, name, code
"""
TAG_CODES_SQL = "SELECT array_agg(code) FROM crm.tag WHERE applies_to = 'client' AND code = ANY($1::text[])"
BLOCKS_SQL = """
SELECT content_id, locale, title, version FROM crm.aa_content
 WHERE kind = 'campaign_block' AND listed ORDER BY locale, title, content_id
"""
BLOCK_SQL = """
SELECT content_id, locale, title, body_markdown FROM crm.aa_content
 WHERE content_id = $1 AND kind = 'campaign_block' AND listed
"""
TOURS_SEARCH_SQL = """
SELECT source_id, title, country_code, duration_days FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND served_state = 'served' AND ($1::text = '' OR title ILIKE '%' || $1 || '%')
 ORDER BY title, source_id
 LIMIT 20
"""
TOUR_TITLES_SQL = """
SELECT source_id, title, served_state FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = ANY($1)
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _excluded(value: Any) -> dict[str, int]:
    data = json.loads(value) if isinstance(value, str) else (value or {})
    return {str(k): int(v) for k, v in data.items()}


async def _feature(ctx: TenantContext, action: str, campaign_id: uuid.UUID | None = None) -> None:
    await entitlements.require_feature(
        ctx, FEATURE, action=action, object_kind="campaign", object_id=str(campaign_id) if campaign_id else None
    )


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    """The editable fields, checked (422 with the field's code)."""
    out: dict[str, Any] = {}
    name = " ".join(str(fields.get("name") or "").split())
    if not 1 <= len(name) <= model.NAME_CHARS:
        raise Invalid("invalid_name", f"1 to {model.NAME_CHARS} characters")
    out["name"] = name
    locale = fields.get("locale") or "en"
    if locale not in model.LOCALES:
        raise Invalid("invalid_locale", "en or vi")
    out["locale"] = locale
    subject = " ".join(str(fields.get("subject") or "").split())
    if len(subject) > model.SUBJECT_CHARS:
        raise Invalid("invalid_subject", f"at most {model.SUBJECT_CHARS} characters")
    out["subject"] = subject
    body = str(fields.get("body") or "").replace("\r\n", "\n")
    if len(body) > model.BODY_CHARS:
        raise Invalid("invalid_body", f"at most {model.BODY_CHARS} characters")
    out["body"] = body
    tours = [str(t).strip() for t in fields.get("aa_tour_ids") or []]
    if len(tours) > model.LIST_ITEMS or any(not model.TOUR_ID.match(t) for t in tours):
        raise Invalid("invalid_aa_tour_ids", f"at most {model.LIST_ITEMS} AA tour ids")
    out["aa_tour_ids"] = sorted(set(tours))
    tags = [str(t).strip() for t in fields.get("audience_tags") or []]
    if len(tags) > model.LIST_ITEMS or any(not model.TAG_CODE.match(t) for t in tags):
        raise Invalid("invalid_audience_tags", f"at most {model.LIST_ITEMS} client tag codes")
    out["audience_tags"] = sorted(set(tags))
    block = fields.get("aa_content_id")
    out["aa_content_id"] = uuid.UUID(str(block)) if block else None
    out["brand_only"] = bool(fields.get("brand_only"))
    mailbox = fields.get("channel_account_id")
    out["channel_account_id"] = uuid.UUID(str(mailbox)) if mailbox else None
    return out


def _may_send_from(ctx: TenantContext, box: Any) -> bool:
    """BYO: the agency mailbox (no member) or the caller's own; never another member's personal mailbox."""
    return box["member_id"] is None or box["member_id"] == ctx.member_id


async def _check_refs(ctx: TenantContext, v: dict[str, Any]) -> None:
    if v["channel_account_id"] is not None:
        box = await ctx.conn.fetchrow(MAILBOX_SQL, v["channel_account_id"])
        # BYO: the agency's mailbox or the member's own; never another member's personal mailbox.
        if box is None or not _may_send_from(ctx, box):
            raise Invalid("invalid_mailbox", "pick the agency mailbox or your own")
    if v["audience_tags"]:
        known = set(await ctx.conn.fetchval(TAG_CODES_SQL, v["audience_tags"]) or [])
        if known != set(v["audience_tags"]):
            raise Invalid("tag_not_found", "a client tag of the agency")
    if v["aa_content_id"] is not None and await ctx.conn.fetchrow(BLOCK_SQL, v["aa_content_id"]) is None:
        raise Invalid("aa_content_not_found", "an AA campaign block that is still listed")


async def _seal(vault: Vault, campaign_id: uuid.UUID, text: str, column: str) -> bytes | None:
    if not text:
        return None
    return await vault.encrypt("campaign", column, campaign_id, text, purpose=MAIL_PURPOSE)


async def _row(ctx: TenantContext, campaign_id: uuid.UUID, *, lock: bool = False) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL if lock else ONE_SQL, campaign_id)
    if row is None:
        raise NotFound("campaign_not_found")
    return row


def _gate_row(v: dict[str, Any]) -> dict[str, Any]:
    return {k: v[k] for k in ("aa_tour_ids", "aa_content_id", "brand_only")}


async def listing(ctx: TenantContext) -> dict[str, Any]:
    require(ctx, Action.MARKETING_VIEW, object_kind="campaign")
    rows = await ctx.conn.fetch(LIST_SQL, PAGE_SIZE)
    ctx.audit(
        "list_view", "allowed", "campaign", reason="campaigns", object_ids=tuple(str(r["campaign_id"]) for r in rows)
    )
    return {
        "feature_allowed": await entitlements.feature_allowed(ctx, FEATURE),
        "can_send": ctx.member_role in ("owner", "agent"),
        "items": [
            {
                "campaign_id": str(r["campaign_id"]),
                "name": r["name"],
                "state": r["state"],
                "hold_code": r["hold_code"],
                "gate_codes": list(r["gate_codes"]),
                "scheduled_at": _iso(r["scheduled_at"]),
                "started_at": _iso(r["started_at"]),
                "finished_at": _iso(r["finished_at"]),
                "recipient_count": r["recipient_count"],
                "excluded": _excluded(r["excluded"]),
                "counts": {k: r[k] for k in ("planned", "queued", "skipped", "sent", "unsubscribed")},
                "version": r["version"],
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ],
    }


async def detail(ctx: TenantContext, vault: Vault, campaign_id: uuid.UUID) -> dict[str, Any]:
    require(ctx, Action.MARKETING_VIEW, object_kind="campaign", object_id=str(campaign_id))
    row = await _row(ctx, campaign_id)
    subject, body = await audience.texts(vault, row)
    tours = list(row["aa_tour_ids"])
    titles = {r["source_id"]: r for r in await ctx.conn.fetch(TOUR_TITLES_SQL, tours)} if tours else {}
    counts = await ctx.conn.fetch(
        "SELECT state, coalesce(outcome_code, '') AS code, count(*)::int AS n FROM crm.campaign_recipient "
        "WHERE campaign_id = $1 GROUP BY 1, 2",
        campaign_id,
    )
    outcomes = await ctx.conn.fetch(
        "SELECT d.status, coalesce(d.gate_result, '') AS code, count(*)::int AS n FROM crm.campaign_recipient r "
        "JOIN crm.dispatch_queue d ON d.dispatch_id = r.dispatch_id WHERE r.campaign_id = $1 GROUP BY 1, 2",
        campaign_id,
    )
    ctx.audit("view", "allowed", "campaign", str(campaign_id))
    return {
        "campaign_id": str(campaign_id),
        "name": row["name"],
        "locale": row["locale"],
        "subject": subject,
        "body": body,
        "channel_account_id": str(row["channel_account_id"]) if row["channel_account_id"] else None,
        "aa_tours": [
            {
                "source_id": t,
                "title": titles[t]["title"] if t in titles else None,
                "served_state": titles[t]["served_state"] if t in titles else None,
            }
            for t in tours
        ],
        "aa_content_id": str(row["aa_content_id"]) if row["aa_content_id"] else None,
        "brand_only": row["brand_only"],
        "audience_tags": list(row["audience_tags"]),
        "state": row["state"],
        "hold_code": row["hold_code"],
        "gate_codes": list(row["gate_codes"]),
        "scheduled_at": _iso(row["scheduled_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "daily_limit": row["daily_limit"],
        "recipient_count": row["recipient_count"],
        "excluded": _excluded(row["excluded"]),
        "recipients": [{"state": r["state"], "code": r["code"] or None, "count": r["n"]} for r in counts],
        "dispatch": [{"status": r["status"], "code": r["code"] or None, "count": r["n"]} for r in outcomes],
        "version": row["version"],
        "created_at": _iso(row["created_at"]),
    }


async def create(ctx: TenantContext, vault: Vault, fields: dict[str, Any], links: audience.Links) -> dict[str, Any]:
    require(ctx, Action.CAMPAIGN_SEND, object_kind="campaign")
    await _feature(ctx, "campaign_create")
    v = _clean(fields)
    await _check_refs(ctx, v)
    campaign_id = uuid.uuid4()
    codes = await audience.gate(ctx, _gate_row(v), v["subject"], v["body"], links)
    await ctx.conn.execute(
        INSERT_SQL, campaign_id, v["name"], v["locale"], await _seal(vault, campaign_id, v["subject"], "subject_enc"),
        await _seal(vault, campaign_id, v["body"], "body_enc"), v["channel_account_id"], v["aa_tour_ids"],
        v["aa_content_id"], v["brand_only"], v["audience_tags"], codes, ctx.member_id,
    )  # fmt: skip
    ctx.audit("campaign_create", "allowed", "campaign", str(campaign_id))
    return {"campaign_id": str(campaign_id), "state": "draft", "gate_codes": codes, "version": 1}


async def update(
    ctx: TenantContext,
    vault: Vault,
    campaign_id: uuid.UUID,
    fields: dict[str, Any],
    version: int,
    links: audience.Links,
) -> dict[str, Any]:
    require(ctx, Action.CAMPAIGN_SEND, object_kind="campaign", object_id=str(campaign_id))
    await _feature(ctx, "campaign_edit", campaign_id)
    row = await _row(ctx, campaign_id, lock=True)
    if row["version"] != version:
        raise Conflict("version_conflict", "the campaign changed: reload it")
    if row["state"] not in model.EDITABLE:
        raise Conflict("campaign_not_editable", "only a draft or a held campaign is edited")
    v = _clean(fields)
    await _check_refs(ctx, v)
    codes = await audience.gate(ctx, _gate_row(v), v["subject"], v["body"], links)
    new_version = await ctx.conn.fetchval(
        UPDATE_SQL, campaign_id, v["name"], v["locale"], await _seal(vault, campaign_id, v["subject"], "subject_enc"),
        await _seal(vault, campaign_id, v["body"], "body_enc"), v["channel_account_id"], v["aa_tour_ids"],
        v["aa_content_id"], v["brand_only"], v["audience_tags"], codes,
    )  # fmt: skip
    ctx.audit("campaign_edit", "allowed", "campaign", str(campaign_id))
    return {"campaign_id": str(campaign_id), "state": row["state"], "gate_codes": codes, "version": new_version}


async def delete(ctx: TenantContext, campaign_id: uuid.UUID) -> dict[str, Any]:
    require(ctx, Action.CAMPAIGN_SEND, object_kind="campaign", object_id=str(campaign_id))
    row = await _row(ctx, campaign_id, lock=True)
    if row["state"] != "draft":
        raise Conflict("campaign_not_draft", "only a draft is deleted; cancel a scheduled campaign")
    await ctx.conn.execute(DELETE_SQL, campaign_id)
    ctx.audit("campaign_delete", "allowed", "campaign", str(campaign_id))
    return {"campaign_id": str(campaign_id), "deleted": True}


async def _prepare(
    ctx: TenantContext, vault: Vault, row: Any, links: audience.Links
) -> tuple[list[str], audience.Audience, int, int, set[uuid.UUID]]:
    subject, body = await audience.texts(vault, row)
    codes = await audience.gate(ctx, row, subject, body, links)
    done = await audience.done_clients(ctx, row["campaign_id"])
    found = await audience.audience(ctx, list(row["audience_tags"]), skip=done)
    return codes, found, await audience.daily_limit(ctx), await audience.max_recipients(ctx), done


async def preview(ctx: TenantContext, vault: Vault, campaign_id: uuid.UUID, links: audience.Links) -> dict[str, Any]:
    """What scheduling now would do: the gate codes, the eligible count, the left-out counts, the days it takes."""
    require(ctx, Action.CAMPAIGN_SEND, object_kind="campaign", object_id=str(campaign_id))
    row = await _row(ctx, campaign_id)
    codes, found, limit, cap, done = await _prepare(ctx, vault, row, links)
    start = await audience.today(ctx)
    days = 0
    if row["channel_account_id"] is not None and found.eligible:
        taken = await audience.used(ctx, row["channel_account_id"], start, other_than=campaign_id)
        days = len(set(model.spread(len(found.eligible), start, limit, taken)))
    return {
        "campaign_id": str(campaign_id),
        "gate_codes": codes,
        "eligible": len(found.eligible),
        "excluded": found.excluded,
        "already_done": len(done),
        "daily_limit": limit,
        "max_recipients": cap,
        "over_cap": len(found.eligible) > cap,
        "days": days,
        "has_mailbox": row["channel_account_id"] is not None,
        "feature_allowed": await entitlements.feature_allowed(ctx, FEATURE),
    }


async def _sending_mailbox(ctx: TenantContext, mailbox: uuid.UUID | None) -> uuid.UUID:
    """The campaign's mailbox, if the caller may schedule it: one of the agency's own (409 ``mailbox_required``), and
    the agency mailbox or the caller's own (409 ``invalid_mailbox``: the editor's check alone is not enough, a
    colleague could schedule a draft written on someone's personal mailbox)."""
    box = await ctx.conn.fetchrow(MAILBOX_SQL, mailbox) if mailbox is not None else None
    if box is None or mailbox is None:
        raise Conflict("mailbox_required", "connect and pick one of the agency's own mailboxes")
    if not _may_send_from(ctx, box):
        raise Conflict("invalid_mailbox", "pick the agency mailbox or your own")
    return mailbox


async def schedule(
    ctx: TenantContext,
    vault: Vault,
    campaign_id: uuid.UUID,
    *,
    at: dt.datetime | None,
    expected: int,
    version: int,
    links: audience.Links,
) -> dict[str, Any]:
    """Draft or held -> scheduled, with the recipients planned over days (see the module docstring)."""
    require(ctx, Action.CAMPAIGN_SEND, object_kind="campaign", object_id=str(campaign_id))
    await _feature(ctx, "campaign_schedule", campaign_id)
    row = await _row(ctx, campaign_id, lock=True)
    if row["version"] != version:
        raise Conflict("version_conflict", "the campaign changed: reload it")
    if row["state"] not in model.EDITABLE:
        raise Conflict("campaign_not_editable", "only a draft or a held campaign is scheduled")
    if not links.client_origin:
        raise Conflict("campaigns_not_configured", "campaign links cannot be made here yet")
    mailbox = await _sending_mailbox(ctx, row["channel_account_id"])
    codes, found, limit, cap, _done = await _prepare(ctx, vault, row, links)
    if codes:
        raise Invalid("content_gate", "the campaign cannot go out as written", codes=codes)
    if not found.eligible:
        raise Conflict("audience_empty", "no client with a marketing yes matches")
    if len(found.eligible) > cap:
        raise Conflict("audience_over_cap", f"at most {cap} clients per campaign: narrow it with client tags")
    if expected != len(found.eligible):
        raise Conflict("count_changed", "the audience changed since the preview", eligible=len(found.eligible))
    now = dt.datetime.now(dt.UTC)
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)  # a time without a zone is UTC (the web sends ISO with Z)
    when = max(at, now) if at is not None else now
    start = await audience.local_day(ctx, when)
    planned = await audience.plan(ctx, campaign_id, mailbox, found.eligible, start, limit)
    new_version = await ctx.conn.fetchval(
        SCHEDULE_SQL, campaign_id, when, limit, len(found.eligible) + len(_done), audience.excluded_json(found.excluded)
    )
    ctx.audit("campaign_schedule", "allowed", "campaign", str(campaign_id), reason=row["state"])
    return {
        "campaign_id": str(campaign_id),
        "state": "scheduled",
        "scheduled_at": when.isoformat(),
        "eligible": len(found.eligible),
        "excluded": found.excluded,
        "daily_limit": limit,
        **planned,
        "version": new_version,
    }


async def cancel(ctx: TenantContext, campaign_id: uuid.UUID, version: int) -> dict[str, Any]:
    require(ctx, Action.CAMPAIGN_SEND, object_kind="campaign", object_id=str(campaign_id))
    row = await _row(ctx, campaign_id, lock=True)
    if row["version"] != version:
        raise Conflict("version_conflict", "the campaign changed: reload it")
    if row["state"] not in model.CANCELLABLE:
        raise Conflict("campaign_not_cancellable", "a sent or cancelled campaign stays as it is")
    status = await ctx.conn.execute(SKIP_PLANNED_SQL, campaign_id)  # "UPDATE <n>"
    skipped = int(status.split()[-1]) if status.split()[-1].isdigit() else 0
    undone = await send.withdraw(ctx, campaign_id, cancelled=True)  # queued or waiting for the mailbox
    new_version = await ctx.conn.fetchval(CANCEL_SQL, campaign_id)
    ctx.audit("campaign_cancel", "allowed", "campaign", str(campaign_id), reason=row["state"])
    return {"campaign_id": str(campaign_id), "state": "cancelled", "skipped": skipped, "undone": undone,
            "version": new_version}  # fmt: skip


async def options(ctx: TenantContext, vault: Vault, links: audience.Links) -> dict[str, Any]:
    """What the editor offers: the member's sending mailboxes (the agency's and their own), the client tags, the AA
    campaign blocks, the limits in force and whether the planner links are allowed here."""
    require(ctx, Action.MARKETING_VIEW, object_kind="campaign")
    boxes = []
    for r in await ctx.conn.fetch(MAILBOXES_SQL, ctx.member_id):
        address = await vault.decrypt(
            "channel_account", "address_enc", r["channel_account_id"], r["address_enc"], purpose=MAIL_PURPOSE
        )
        boxes.append(
            {
                "channel_account_id": str(r["channel_account_id"]),
                "scope": "mine" if r["member_id"] else "agency",
                "kind": r["kind"],
                "status": r["status"],
                "address": address,
            }
        )
    return {
        "feature_allowed": await entitlements.feature_allowed(ctx, FEATURE),
        "mailboxes": boxes,
        "tags": [dict(r) for r in await ctx.conn.fetch(TAGS_SQL)],
        "blocks": [
            {"content_id": str(r["content_id"]), "locale": r["locale"], "title": r["title"], "version": r["version"]}
            for r in await ctx.conn.fetch(BLOCKS_SQL)
        ],
        "daily_limit": await audience.daily_limit(ctx),
        "max_recipients": await audience.max_recipients(ctx),
        "client_origin": links.client_origin,
        "tripplanner_origin": links.tripplanner_origin,
    }


async def block(ctx: TenantContext, content_id: uuid.UUID) -> dict[str, Any]:
    """One AA campaign block's text (the editor copies it into the campaign)."""
    require(ctx, Action.MARKETING_VIEW, object_kind="aa_content", object_id=str(content_id))
    row = await ctx.conn.fetchrow(BLOCK_SQL, content_id)
    if row is None:
        raise NotFound("aa_content_not_found")
    return {"content_id": str(content_id), "locale": row["locale"], "title": row["title"], "body": row["body_markdown"]}


async def tours(ctx: TenantContext, q: str) -> dict[str, Any]:
    """Served AA tours by title (the editor's picker; the CRM's own catalogue cache, no CIS call)."""
    require(ctx, Action.MARKETING_VIEW, object_kind="aa_tour")
    text = " ".join((q or "").split())[:80]
    pattern = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = await ctx.conn.fetch(TOURS_SEARCH_SQL, pattern)
    return {"items": [dict(r) for r in rows]}
