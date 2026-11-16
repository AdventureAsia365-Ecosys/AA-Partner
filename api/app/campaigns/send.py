"""``crm_campaign_tick``: the hourly send pass of the agencies' campaigns (AA-968, card CRM-36; design v3 R14).

Queued **due now** once per UTC hour by :class:`HourlyCampaignTick` (never ahead: the DB Migrate idle check counts a
queued row as busy; not deploy-gate exempt). Per agency, in one :func:`app.tenancy.system_tx` (actor
``job:crm_campaign_tick``), each live campaign (``scheduled`` / ``sending``, its time come) in a savepoint of its own
(one campaign's error never stops the others; it is counted ``failed``):

1. **Gates, again on each send day**: the agency's plan still allows ``campaigns`` (else held ``feature_off``); the
   client host is configured (the unsubscribe link, else held ``not_configured``); the campaign's mailbox is one of
   the agency's own, not deleted (else held ``no_mailbox``: BYO, never another sender); the d21 content gate passes
   (an AA tour no longer served -> held ``aa_tour_retired``; any other code -> held ``content_gate``).
2. **Today's clients**: the planned rows due by today (agency timezone), at most the mailbox's room today (the daily
   limit, ``crm.campaign.mailbox_daily_limit`` or the agency's own value, minus the campaign mails it queued today):
   the rest wait for the next day (card E). Each client is checked again: gone or archived (``archived``), no address
   (``no_email``), no current marketing yes (``no_consent``, card R), the address on the suppression list
   (``suppressed``): the row is ``skipped`` with that code. Otherwise one outbound ``crm.message`` in the client's
   conversation (subject and text with the merge fields, then the unsubscribe lines with a fresh one-click token, all
   under the mail DEK) from the campaign's mailbox, and one ``crm_dispatch`` row (purpose ``marketing``, origin
   ``system``): the dispatcher's gate decides the send (archived, trashed, consent, suppression) and the AA-945 sender
   sends through the agency's own mailbox, with the List-Unsubscribe headers (RFC 8058).
3. The campaign is ``sending`` while planned rows are left, ``sent`` when none is.

:func:`pull_retired` is the CRM fan-out's step (:mod:`app.catalog.retired`): a campaign that names an AA tour AA
retired is held ``aa_tour_retired`` at once, before its next send day.

Payload and result are ids and counts only. A locked or offboarding agency is skipped.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app import entitlements
from app.audit_export import registry
from app.campaigns import audience, model
from app.dispatch import queue as dispatch
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import CAMPAIGN_TICK
from app.jobs.queue import Job
from app.mail import threads
from app.proposals import text_render
from app.tenancy import AnyContext, SystemContext, system_tx
from app.vault import KeyProvider, Vault
from app.vault.errors import DecryptionDenied

log = logging.getLogger(__name__)

ACTOR = "job:crm_campaign_tick"
MAIL_PURPOSE = "mail"  # the agency's mail DEK (the AA-945 messages, the List-Unsubscribe URL)
SCHEDULER = "scheduler:crm-campaign-tick"
MAX_ATTEMPTS = 3  # technical-constant: job retries of one hourly pass (the next hour runs anyway)
HISTORY_DAYS = 2  # technical-constant: finished tick rows pruned after 2 days (queue hygiene, like the workflow tick)

LIVE_SQL = """
SELECT campaign_id, name, locale, subject_enc, body_enc, channel_account_id, aa_tour_ids, aa_content_id, brand_only,
       state, scheduled_at
  FROM crm.campaign
 WHERE state IN ('scheduled', 'sending') AND scheduled_at <= now() AND deleted_at IS NULL
 ORDER BY scheduled_at, campaign_id
"""
MAILBOX_SQL = "SELECT channel_account_id FROM crm.channel_account WHERE channel_account_id = $1 AND deleted_at IS NULL"
HOLD_SQL = """
UPDATE crm.campaign SET state = 'held', hold_code = $2, gate_codes = $3, version = version + 1
 WHERE campaign_id = $1 AND state IN ('scheduled', 'sending')
"""
DUE_SQL = """
SELECT r.campaign_recipient_id, r.traveler_id,
       t.deleted_at IS NOT NULL AS gone, t.archived_at IS NOT NULL AS archived, t.email_enc IS NOT NULL AS has_email,
       t.email_bidx, t.given_name_enc, t.display_name_enc,
       (SELECT c.value FROM crm.consent c WHERE c.traveler_id = r.traveler_id AND c.purpose = 'marketing'
         ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC LIMIT 1) AS marketing,
       EXISTS (SELECT 1 FROM crm.campaign_suppression s
                WHERE s.email_bidx = t.email_bidx AND s.deleted_at IS NULL) AS suppressed
  FROM crm.campaign_recipient r
  JOIN crm.traveler t ON t.traveler_id = r.traveler_id
 WHERE r.campaign_id = $1 AND r.state = 'planned' AND r.send_on <= $2 AND r.deleted_at IS NULL
 ORDER BY r.send_on, r.campaign_recipient_id
 LIMIT $3
   FOR UPDATE OF r
"""
SKIP_SQL = """
UPDATE crm.campaign_recipient SET state = 'skipped', outcome_code = $2
 WHERE campaign_recipient_id = $1 AND state = 'planned'
"""
QUEUED_SQL = """
UPDATE crm.campaign_recipient
   SET state = 'queued', message_id = $2, dispatch_id = $3, queued_at = now(), unsubscribe_hash = $4,
       unsubscribe_url_enc = $5
 WHERE campaign_recipient_id = $1 AND state = 'planned'
"""
MAIL_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, sender_kind, channel_account_id, subject_enc,
                         body_enc, match_method, occurred_at)
VALUES ($1, $2, 'out', 'queued', 'agency_mailbox', $3, $4, $5, 'compose', now())
"""
TOUCH_SQL = "UPDATE crm.conversation SET last_message_at = now() WHERE conversation_id = $1"
LEFT_SQL = "SELECT count(*)::int FROM crm.campaign_recipient WHERE campaign_id = $1 AND state = 'planned'"
SENDING_SQL = """
UPDATE crm.campaign SET state = 'sending', started_at = coalesce(started_at, now())
 WHERE campaign_id = $1 AND state = 'scheduled'
"""
SENT_SQL = """
UPDATE crm.campaign SET state = 'sent', started_at = coalesce(started_at, now()), finished_at = now(),
       version = version + 1
 WHERE campaign_id = $1 AND state IN ('scheduled', 'sending')
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
RETIRED_SQL = """
UPDATE crm.campaign SET state = 'held', hold_code = 'aa_tour_retired', gate_codes = $2, version = version + 1
 WHERE state IN ('scheduled', 'sending') AND deleted_at IS NULL AND $1 = ANY (aa_tour_ids)
RETURNING campaign_id
"""


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def pull_retired(ctx: AnyContext, source_id: str) -> int:
    """Hold every scheduled or sending campaign of this agency that names the retired AA tour (the fan-out's step;
    the same tour run twice holds nothing new). Returns how many it held."""
    held = [r["campaign_id"] for r in await ctx.conn.fetch(RETIRED_SQL, source_id, [model.TOUR_NOT_SERVED])]
    for campaign_id in held:
        ctx.audit("campaign_hold", "allowed", "campaign", str(campaign_id), reason=model.HOLD_RETIRED)
    return len(held)


async def _hold(ctx: AnyContext, campaign_id: uuid.UUID, code: str, gate_codes: list[str] | None = None) -> None:
    await ctx.conn.execute(HOLD_SQL, campaign_id, code, gate_codes or [])
    ctx.audit("campaign_hold", "allowed", "campaign", str(campaign_id), reason=code)


async def _first_name(vault: Vault, row: Any) -> str | None:
    if row["given_name_enc"] is not None:
        return await vault.decrypt("traveler", "given_name_enc", row["traveler_id"], row["given_name_enc"])
    if row["display_name_enc"] is not None:
        name = await vault.decrypt("traveler", "display_name_enc", row["traveler_id"], row["display_name_enc"])
        return (name.split() or [None])[0]
    return None


async def _queue_one(
    ctx: AnyContext,
    vault: Vault,
    campaign: Any,
    row: Any,
    texts: tuple[str, str],
    agency: str,
    client_origin: str,
) -> str:
    """One client of today: ``queued`` or the skip code."""
    if row["gone"] or row["archived"]:
        code = "archived"
    else:
        code = audience.exclusion(row)
    if code is not None:
        await ctx.conn.execute(SKIP_SQL, row["campaign_recipient_id"], code)
        return code
    subject, body = texts
    values: dict[str, str | None] = dict.fromkeys(text_render.MERGE_FIELDS)
    values["agency_name"] = agency
    values["traveller_first_name"] = await _first_name(vault, row)
    merged_subject, _ = text_render.merge(subject, values)
    merged_body, _ = text_render.merge(body, values)
    token = secrets.token_urlsafe(32)
    text = merged_body + model.footer(campaign["locale"], agency, model.page_url(client_origin, token))
    message_id = uuid.uuid4()
    conversation_id = await threads.conversation_for(ctx, None, row["traveler_id"])
    subject_enc = await vault.encrypt(
        "message", "subject_enc", message_id, merged_subject or subject, purpose=MAIL_PURPOSE
    )
    body_enc = await vault.encrypt("message", "body_enc", message_id, text, purpose=MAIL_PURPOSE)
    await ctx.conn.execute(MAIL_SQL, message_id, conversation_id, campaign["channel_account_id"], subject_enc, body_enc)
    await ctx.conn.execute(TOUCH_SQL, conversation_id)
    queued = await dispatch.enqueue_message(
        ctx, traveler_id=row["traveler_id"], channel="email", purpose="marketing", origin="system",
        message_id=message_id,
    )  # fmt: skip
    url_enc = await vault.encrypt(
        "campaign_recipient", "unsubscribe_url_enc", row["campaign_recipient_id"],
        model.one_click_url(client_origin, token), purpose=MAIL_PURPOSE,
    )  # fmt: skip
    await ctx.conn.execute(QUEUED_SQL, row["campaign_recipient_id"], message_id, uuid.UUID(queued["dispatch_id"]),
                           token_hash(token), url_enc)  # fmt: skip
    return "queued"


async def run_campaign(
    ctx: AnyContext, vault: Vault, campaign: Any, links: audience.Links, *, limit: int, agency: str
) -> Counter[str]:
    """Gates, then today's clients of one live campaign (see the module docstring)."""
    counts: Counter[str] = Counter()
    cid = campaign["campaign_id"]
    if not await entitlements.feature_allowed(ctx, "campaigns"):
        await _hold(ctx, cid, model.HOLD_FEATURE)
        counts["held"] += 1
        return counts
    if not links.client_origin:
        await _hold(ctx, cid, model.HOLD_NOT_CONFIGURED)
        counts["held"] += 1
        return counts
    mailbox = campaign["channel_account_id"]
    if mailbox is None or await ctx.conn.fetchval(MAILBOX_SQL, mailbox) is None:
        await _hold(ctx, cid, model.HOLD_MAILBOX)
        counts["held"] += 1
        return counts
    texts = await audience.texts(vault, campaign)
    codes = await audience.gate(ctx, campaign, *texts, links)
    hold = model.hold_for(codes)
    if hold is not None:
        await _hold(ctx, cid, hold, codes)
        counts["held"] += 1
        return counts
    today = await audience.today(ctx)
    room = await audience.day_room(ctx, mailbox, today, limit)
    rows = await ctx.conn.fetch(DUE_SQL, cid, today, room) if room > 0 else []
    for row in rows:
        counts[await _queue_one(ctx, vault, campaign, row, texts, agency, links.client_origin)] += 1
    if counts["queued"]:
        ctx.audit("campaign_send", "allowed", "campaign", str(cid), reason=f"queued_{min(counts['queued'], 99999)}")
    if await ctx.conn.fetchval(LEFT_SQL, cid) == 0:
        await ctx.conn.execute(SENT_SQL, cid)
        counts["finished"] += 1
    else:
        await ctx.conn.execute(SENDING_SQL, cid)
        counts["waiting"] += 1  # clients left for a later day
    return counts


async def run_agency(ctx: SystemContext, vault: Vault, links: audience.Links) -> Counter[str]:
    totals: Counter[str] = Counter()
    campaigns = await ctx.conn.fetch(LIVE_SQL)
    if not campaigns:
        return totals
    limit = await audience.daily_limit(ctx)
    agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id) or ""
    for campaign in campaigns:
        try:
            async with ctx.conn.transaction():  # a savepoint: one campaign's error leaves the others
                totals.update(await run_campaign(ctx, vault, campaign, links, limit=limit, agency=agency))
        except (ServiceError, DecryptionDenied, asyncpg.PostgresError) as exc:
            totals["failed"] += 1
            log.error("campaign_tick_campaign_failed error_class=%s", type(exc).__name__)
    return totals


def build_handler(keys: KeyProvider | None, links: audience.Links) -> Any:
    """The handler; the agency's "today" is read in its own transaction (its timezone)."""

    async def crm_campaign_tick(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        if keys is None:
            log.error("campaign_tick_not_configured reason=key_provider")
            return {"agency_count": 0, "reason_code": "key_provider_not_configured"}
        totals: Counter[str] = Counter()
        done = skipped = failed = 0
        for agency in await registry.agencies(pool):
            try:
                async with system_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                    counts = await run_agency(ctx, Vault(keys, ctx), links)
            except (Forbidden, NotFound):
                skipped += 1
                continue
            except (ServiceError, DecryptionDenied, asyncpg.PostgresError) as exc:
                failed += 1
                log.error("campaign_tick_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
                continue
            done += 1
            totals.update(counts)
        return {
            "agency_count": done,
            "skipped_count": skipped,
            "failed_count": failed,
            **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
        }

    return crm_campaign_tick


def schedule_key(moment: datetime) -> str:
    return f"{CAMPAIGN_TICK}:schedule:{moment.strftime('%Y-%m-%dT%H')}"


class HourlyCampaignTick:
    """Worker maintenance hook: one ``crm_campaign_tick`` per UTC hour, due now (the HourlyWorkflowTick pattern)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            CAMPAIGN_TICK,
            {"purpose": "hourly_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, CAMPAIGN_TICK, HISTORY_DAYS)
        self._done_slot = key
        return job_id
