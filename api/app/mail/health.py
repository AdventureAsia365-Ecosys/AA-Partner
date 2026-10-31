"""Mailbox health (AA-945; errata S247 §2 "nightly token or login check", R24 "nightly checks").

- ``crm_mail_health`` (daily, due-now hook :class:`DailyMailHealthSchedule` from ``JOB_RETENTION_UTC_HOUR``, key per
  UTC day; not deploy-gate exempt): every agency's mailboxes with a credential get one cheap provider call (token
  refresh; SMTP login + NOOP). Passing -> ``connected`` (a mailbox that was not connected gets its held messages back in
  the queue); a dead token or refused login -> ``disconnected`` + one alert to the owners; unreachable ->
  ``needs_attention`` (``unreachable``), tried again the next night. Counts only.
- :func:`test_mailbox` is the Settings > Mailbox **Test** button: the same check now, then a short test mail from the
  mailbox to the member's own address (when the partner directory knows it). It never goes through AA's SES.

The credential is opened by :func:`app.mail.sender.load_secret` (``Vault.decrypt_secret`` is lint-allowed in
:mod:`app.mail.sender` only).
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
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import MAIL_HEALTH
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.mail import compose
from app.mail.providers import AuthRevoked, ProviderError, Rejected
from app.mail.recipients import deliverable
from app.mail.sender import load_secret, store_rotated
from app.mail.state import MailRuntime, alert_owners, mark_connected, mark_disconnected
from app.members.mail import mailbox_test_message
from app.tenancy import AnyContext, system_tx
from app.vault import Vault

log = logging.getLogger(__name__)

ACTOR = "job:crm_mail_health"
SCHEDULER = "scheduler:crm-mail-health"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
PURPOSE = "mail"

MAILBOXES_SQL = """
SELECT a.channel_account_id, a.kind, a.status, a.credential_id, a.address_enc, ic.secret_enc
  FROM crm.channel_account a
  JOIN crm.integration_credential ic ON ic.integration_credential_id = a.credential_id
 WHERE a.deleted_at IS NULL
 ORDER BY a.channel_account_id
"""
ONE_MAILBOX_SQL = MAILBOXES_SQL.replace(
    "WHERE a.deleted_at IS NULL", "WHERE a.deleted_at IS NULL AND a.channel_account_id = $1"
)
ATTENTION_MAILBOX_SQL = """
UPDATE crm.channel_account SET status = 'needs_attention', last_token_check_at = now()
 WHERE channel_account_id = $1 AND status = 'connected'
"""
ATTENTION_CREDENTIAL_SQL = """
UPDATE crm.integration_credential SET status = 'needs_attention', failure_class = 'unreachable', last_checked_at = now()
 WHERE integration_credential_id = $1 AND status = 'connected'
"""
TEST_RESULT_SQL = """
UPDATE crm.integration_credential SET last_test_at = now(), last_test_result = $2 WHERE integration_credential_id = $1
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"


async def check_row(ctx: AnyContext, runtime: MailRuntime, row: Any) -> str:
    """One mailbox: ``ok``, ``auth_revoked`` / ``permission_missing`` (disconnected), ``unreachable``."""
    outcome, _ = await _check(ctx, runtime, row)
    return outcome


async def _check(ctx: AnyContext, runtime: MailRuntime, row: Any) -> tuple[str, dict[str, Any] | None]:
    """:func:`check_row`, plus the credential that is current afterwards (a rotated refresh token is stored at once)."""
    provider = runtime.providers.get(row["kind"])
    if provider is None or runtime.keys is None:
        return "not_configured", None
    vault = Vault(runtime.keys, ctx)
    secret = await load_secret(vault, row["credential_id"], row["secret_enc"])
    try:
        rotated = await provider.check(secret)
    except AuthRevoked as exc:
        if await mark_disconnected(ctx, row["channel_account_id"], row["credential_id"], exc.code):
            await alert_owners(ctx, runtime)
        return exc.code, None
    except ProviderError:
        await ctx.conn.execute(ATTENTION_MAILBOX_SQL, row["channel_account_id"])
        await ctx.conn.execute(ATTENTION_CREDENTIAL_SQL, row["credential_id"])
        return "unreachable", None
    await store_rotated(ctx, vault, row["credential_id"], rotated)
    requeued = await mark_connected(ctx, row["channel_account_id"], row["credential_id"])
    return ("ok_requeued" if requeued else "ok"), rotated or secret


async def run_agency(ctx: AnyContext, runtime: MailRuntime) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in await ctx.conn.fetch(MAILBOXES_SQL):
        counts[await check_row(ctx, runtime, row)] += 1
    return counts


def build_handler(runtime: MailRuntime) -> Handler:
    async def mail_health(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
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
                log.error("mail_health_failed tenant_id=%s error=%s", agency.tenant_id, code)
                continue
            done += 1
            totals.update(counts)
        return {
            "agency_count": done,
            "skipped_count": skipped,
            "failed_count": failed,
            **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
        }

    return mail_health


async def test_mailbox(
    ctx: AnyContext, runtime: MailRuntime, channel_account_id: uuid.UUID, to_address: str | None
) -> dict[str, Any]:
    """Settings > Mailbox Test: check now, then a test mail to the member's own address."""
    row = await ctx.conn.fetchrow(ONE_MAILBOX_SQL, channel_account_id)
    if row is None:
        raise NotFound("mailbox_not_found")
    outcome, secret = await _check(ctx, runtime, row)
    sent = False
    # AA-1014: never a test mail to a .invalid address (it only bounces back to the mailbox).
    to_address = (deliverable([to_address]) or [None])[0] if to_address else None
    if outcome.startswith("ok") and secret is not None and to_address and runtime.keys is not None:
        vault = Vault(runtime.keys, ctx)
        from_address = await vault.decrypt(
            "channel_account", "address_enc", channel_account_id, row["address_enc"], purpose=PURPOSE
        )
        agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id) or ""
        note = mailbox_test_message(to_address, agency=agency)
        built = compose.build(
            from_address=from_address,
            from_name=agency,
            to_address=to_address,
            subject=note.subject,
            text=note.text,
            mail_token=None,
        )
        try:
            delivery = await runtime.providers[row["kind"]].send(built.raw, secret, [to_address])
            await store_rotated(ctx, vault, row["credential_id"], delivery.rotated_secret)
            sent = True
        except Rejected as exc:
            outcome = exc.code
        except ProviderError as exc:
            outcome = exc.code
    result = "ok" if outcome.startswith("ok") else "failed"
    await ctx.conn.execute(TEST_RESULT_SQL, row["credential_id"], result)
    ctx.audit("mailbox_test", "allowed", "channel_account", str(channel_account_id), reason=outcome)
    return {"result": result, "check": outcome, "test_mail_sent": sent}


def schedule_key(day: date) -> str:
    return f"{MAIL_HEALTH}:schedule:{day.isoformat()}"


class DailyMailHealthSchedule:
    """Worker maintenance hook: one ``crm_mail_health`` run per UTC day from ``utc_hour``, due now, never ahead."""

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
            MAIL_HEALTH,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(now.date()),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, MAIL_HEALTH, HISTORY_DAYS)
        self._done_day = now.date()
        return job_id
