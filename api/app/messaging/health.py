"""SMS / WhatsApp connection health and templates (AA-958; BYO errata S247 §2: "Test button: sends a test SMS /
approved template to the owner's own phone"; "Health check: nightly account read; delivery error codes surfaced in
plain words"; design §5.4 "the CRM reads approved templates").

- :func:`test_connection` is the Settings > Channels **Test** button: the account read now, then (when the owner gave
  their own number) one test message from the agency's sender to it: a short text on SMS, an **approved template** on
  WhatsApp (the one the owner chose, else the first approved service template, else the first approved one; each
  value filled with ``Test``), because free text outside the owner's own 24-hour window is accepted by Twilio and then
  fails later (63016). No approved template: 409 ``template_required`` before anything is called. The webhook is
  checked by a reply: the answer says ``inbound_check: pending`` and Settings shows it ``ok`` once a message arrived
  after the test (``last_inbound_at`` >= ``last_test_at``, :func:`app.messaging.state.inbound_check`). Nothing is
  stored as a client message.
- :func:`sync_templates` reads the templates of the agency's account (Content API) into ``crm.channel_template``:
  every one the provider lists is inserted or updated, the rest deleted. Run by ``crm_messaging_templates`` (one agency,
  queued on connect and on Refresh) and by the daily check.
- ``crm_messaging_health`` (daily, due-now hook :class:`DailyMessagingHealthSchedule` from ``JOB_RETENTION_UTC_HOUR``,
  key per UTC day; not deploy-gate exempt): every agency's connection gets the account read; passing -> ``connected``
  and its templates synced; a refused token or a closed account -> ``disconnected`` + one alert to the owners;
  anything else -> ``needs_attention`` with the failure class, tried again the next night. Counts only.

The credential is opened by :func:`app.messaging.sender.load_secret` (``Vault.decrypt_secret`` is lint-allowed in
:mod:`app.messaging.sender` only).
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from app.audit_export import registry
from app.errors import Conflict, Forbidden, Invalid, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import MESSAGING_HEALTH
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.messaging import state
from app.messaging.providers import AuthRevoked, ProviderError, Rejected, TextOut, placeholders, render
from app.messaging.sender import load_secret
from app.tenancy import AnyContext, system_tx
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone

log = logging.getLogger(__name__)

ACTOR = "job:crm_messaging_health"
TEMPLATES_ACTOR = "job:crm_messaging_templates"
SCHEDULER = "scheduler:crm-messaging-health"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
TEST_TEXT = "AA Partner: this is a test message. Your SMS / WhatsApp connection works."
TEST_VALUE = "Test"
# The WhatsApp test template: the chosen one ($1), else a service one (utility / authentication) first.
TEST_TEMPLATE_SQL = """
SELECT content_sid, body, variables FROM crm.channel_template
 WHERE approval = 'approved' AND deleted_at IS NULL AND ($1::text IS NULL OR content_sid = $1)
 ORDER BY (upper(coalesce(category, '')) IN ('UTILITY', 'AUTHENTICATION')) DESC, name, language
 LIMIT 1
"""

UPSERT_TEMPLATE_SQL = """
INSERT INTO crm.channel_template (messaging_connection_id, content_sid, name, language, category, approval, body,
                                  variables, synced_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], now())
ON CONFLICT (tenant_id, content_sid) DO UPDATE
   SET messaging_connection_id = EXCLUDED.messaging_connection_id, name = EXCLUDED.name,
       language = EXCLUDED.language, category = EXCLUDED.category, approval = EXCLUDED.approval,
       body = EXCLUDED.body, variables = EXCLUDED.variables, synced_at = now()
"""
DELETE_STALE_SQL = "DELETE FROM crm.channel_template WHERE NOT (content_sid = ANY($1::text[]))"
TEST_RESULT_SQL = """
UPDATE crm.integration_credential SET last_test_at = now(), last_test_result = $2 WHERE integration_credential_id = $1
"""


async def _secret(ctx: AnyContext, runtime: state.MessagingRuntime, row: Any) -> dict[str, Any]:
    if runtime.keys is None:
        raise PermanentError("keys_not_configured")
    return await load_secret(Vault(runtime.keys, ctx), row["credential_id"], row["secret_enc"])


async def check_row(ctx: AnyContext, runtime: state.MessagingRuntime, row: Any) -> str:
    """The account read: ``ok``, ``auth_revoked`` (disconnected, owners alerted once) or a failure class
    (``needs_attention``)."""
    provider = runtime.providers.get(row["provider"])
    if provider is None or runtime.keys is None:
        return "not_configured"
    secret = await _secret(ctx, runtime, row)
    try:
        await provider.check(secret)
    except AuthRevoked as exc:
        if await state.mark_disconnected(ctx, row["credential_id"], exc.code):
            await state.alert_owners(ctx, runtime)
        return exc.code
    except Rejected as exc:
        await state.mark_attention(ctx, row["credential_id"], exc.code)
        return exc.code
    except ProviderError as exc:
        code = exc.code if exc.code in state.FAILURE_CLASSES else "unreachable"
        await state.mark_attention(ctx, row["credential_id"], code)
        return code
    await state.mark_connected(ctx, row["credential_id"])
    return "ok"


async def sync_templates(ctx: AnyContext, runtime: state.MessagingRuntime, row: Any) -> dict[str, int]:
    provider = runtime.providers.get(row["provider"])
    if provider is None:
        return {"template_count": 0}
    found = await provider.templates(await _secret(ctx, runtime, row))
    for t in found:
        await ctx.conn.execute(
            UPSERT_TEMPLATE_SQL,
            row["messaging_connection_id"],
            t.content_sid,
            t.name,
            t.language,
            t.category,
            t.approval,
            t.body,
            list(t.variables),
        )
    await ctx.conn.execute(DELETE_STALE_SQL, [t.content_sid for t in found])
    return {
        "template_count": len(found),
        "approved_count": sum(1 for t in found if t.approval == "approved"),
    }


async def _test_template(ctx: AnyContext, content_sid: str | None) -> dict[str, Any]:
    """(content_sid, rendered body, values) of the WhatsApp test template; 409 ``template_required`` without one."""
    found = await ctx.conn.fetchrow(TEST_TEMPLATE_SQL, content_sid)
    if found is None:
        raise Conflict(
            "template_required",
            "a WhatsApp test needs an approved template: approve one in Twilio, then Refresh the templates",
        )
    names = list(found["variables"] or []) or list(placeholders(found["body"]))
    values = {name: TEST_VALUE for name in names}
    return {"content_sid": found["content_sid"], "body": render(found["body"], values), "variables": values or None}


async def test_connection(
    ctx: AnyContext,
    runtime: state.MessagingRuntime,
    *,
    channel: str | None,
    to: str | None,
    content_sid: str | None = None,
) -> dict[str, Any]:
    """Settings > Channels Test: the account read now, then a test message to the owner's own number (WhatsApp: an
    approved template)."""
    row = await state.connection(ctx)
    if row is None:
        raise NotFound("messaging_not_connected")
    number = None
    if to:
        try:
            number = normalise_phone(to.strip())
        except NormalisationError:
            raise Invalid("invalid_test_number", "an international number starting with +") from None
    template = await _test_template(ctx, content_sid) if number and channel == "whatsapp" else None
    outcome = await check_row(ctx, runtime, row)
    sent = False
    if outcome == "ok" and number and channel in state.CHANNELS:
        sender = state.sender_for(row, channel)
        if sender is None:
            outcome = "channel_not_configured"
        else:
            out = TextOut(
                channel=channel,
                to=number,
                sender=sender,
                status_callback=runtime.status_url(row["hook_id"]),
                reference=f"test:{uuid.uuid4()}",
                body=template["body"] if template else TEST_TEXT,
                content_sid=template["content_sid"] if template else None,
                variables=template["variables"] if template else None,
            )
            try:
                await runtime.providers[row["provider"]].send(await _secret(ctx, runtime, row), out)
                sent = True
            except ProviderError as exc:
                outcome = exc.code
    result = "ok" if outcome == "ok" else "failed"
    await ctx.conn.execute(TEST_RESULT_SQL, row["credential_id"], result)
    ctx.audit("messaging_test", "allowed", "messaging_connection", str(row["messaging_connection_id"]), reason=outcome)
    return {
        "result": result,
        "check": outcome,
        "test_message_sent": sent,
        "template": template["content_sid"] if template and sent else None,
        # The webhook is proved by a reply to the test (Settings shows it ok once a message arrived after it).
        "inbound_check": "pending" if sent else None,
    }


def build_templates_handler(runtime: state.MessagingRuntime) -> Handler:
    """``crm_messaging_templates``: one agency's template sync (payload ``tenant_id``)."""

    async def messaging_templates(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
        except (KeyError, ValueError):
            raise PermanentError("bad_payload") from None
        try:
            async with system_tx(pool, tenant_id, actor=TEMPLATES_ACTOR) as ctx:
                row = await state.connection(ctx)
                if row is None:
                    return {"status": "skipped", "reason_code": "not_connected"}
                try:
                    counts = await sync_templates(ctx, runtime, row)
                except AuthRevoked as exc:
                    if await state.mark_disconnected(ctx, row["credential_id"], exc.code):
                        await state.alert_owners(ctx, runtime)
                    return {"status": "failed", "reason_code": exc.code}
                except Rejected as exc:
                    return {"status": "failed", "reason_code": exc.code}
                return {"status": "done", **counts}
        except (Forbidden, NotFound):
            return {"status": "skipped", "reason_code": "tenant_inactive"}

    return messaging_templates


async def run_agency(ctx: AnyContext, runtime: state.MessagingRuntime) -> Counter[str]:
    counts: Counter[str] = Counter()
    row = await state.connection(ctx)
    if row is None:
        return counts
    outcome = await check_row(ctx, runtime, row)
    counts[outcome] += 1
    if outcome == "ok":
        try:
            await sync_templates(ctx, runtime, row)
            counts["templates_synced"] += 1
        except ProviderError:
            counts["templates_failed"] += 1
    return counts


def build_health_handler(runtime: state.MessagingRuntime) -> Handler:
    async def messaging_health(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        totals: Counter[str] = Counter()
        done = skipped = failed = 0
        for agency in await registry.agencies(pool):
            try:
                async with system_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                    counts = await run_agency(ctx, runtime)
            except (Forbidden, NotFound):
                skipped += 1
                continue
            except Exception as exc:  # noqa: BLE001 — one agency's error must not skip every later agency
                failed += 1
                code = exc.code if isinstance(exc, ServiceError) else type(exc).__name__
                log.error("messaging_health_failed tenant_id=%s error=%s", agency.tenant_id, code)
                continue
            done += 1
            totals.update(counts)
        return {
            "agency_count": done,
            "skipped_count": skipped,
            "failed_count": failed,
            **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
        }

    return messaging_health


def schedule_key(day: date) -> str:
    return f"{MESSAGING_HEALTH}:schedule:{day.isoformat()}"


class DailyMessagingHealthSchedule:
    """Worker maintenance hook: one ``crm_messaging_health`` run per UTC day from ``utc_hour``, due now, never
    ahead."""

    def __init__(self, utc_hour: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._utc_hour = utc_hour
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        now = self._now()
        if now.hour < self._utc_hour or self._done_day == now.date():
            return None
        job_id = await queue.enqueue(
            pool,
            MESSAGING_HEALTH,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(now.date()),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, MESSAGING_HEALTH, HISTORY_DAYS)
        self._done_day = now.date()
        return job_id
