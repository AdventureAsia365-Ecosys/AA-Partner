"""The public side of the inquiry form (C-01, card CRM-08): read a form, store one submission. No token.

Called by ``app.routers.public`` (``/crm/public/v1``) inside :func:`app.tenancy.intake_tx`, the only context that may
read an agency's form by its URL key (crm-005 policy ``intake_form_public_lookup``).

:func:`submit`, in order (card R test: "honeypot filled or no consent -> no row, count-only log"):

1. the Origin must be the client host or one of the form's allowed origins (``INTAKE_PAGE_ORIGINS``), else 403
   ``origin_not_allowed``; without configured page origins outside test / local, 503 ``intake_not_configured``;
2. the form's honeypot field holds anything -> **202 as if stored**, nothing written, one count-only log line (the bot
   learns nothing);
3. the contact consent box is not ticked -> **422** ``consent_required``, nothing written, one count-only log line;
4. any other invalid answer -> 422 ``invalid_submission`` with the field codes (never the values);
5. the form's hourly quota (``form_quota``, ``RATE_LIMIT_INTAKE_FORM_PER_HOUR``) is charged only now, by a submission
   that passed every check above, so forged-origin, honeypot or invalid requests cannot use up a form's allowance
   (429 ``form_rate_limited``, nothing written);
6. otherwise the normalised answers are encrypted (vault DEK, AAD = the submission id) into ``crm.form_submission``
   and a ``crm_intake_process`` job (ids only) is queued **in the same transaction**; a repeated
   ``client_submission_id`` stores nothing new (idempotent) and answers the same 202.

The response never carries an id, a name or whether the e-mail is known (no account probing).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.errors import Forbidden, Invalid, RateLimited, Unavailable
from app.intake import stage1
from app.jobs import queue
from app.jobs.kinds import INTAKE_PROCESS
from app.settings import Settings
from app.tenancy import AnyContext, intake_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

# Drops since the process started, by reason (count-only: no payload, no address, no e-mail is ever logged).
DROPS: Counter[str] = Counter()

FORM_SQL = """
SELECT d.schema, t.display_name, t.default_locale
  FROM crm.form_definition d, crm.tenant t
 WHERE d.form_definition_id = $1 AND t.tenant_id = $2
"""
INSERT_SQL = """
INSERT INTO crm.form_submission (submission_id, intake_form_id, stage, schema_key, payload_enc, idempotency_key,
                                 hidden_answers_dropped)
VALUES ($1, $2, 's1', $3, $4, $5, $6)
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING submission_id
"""
MAX_JOB_ATTEMPTS = 5


@dataclass(frozen=True)
class Submitted:
    stored: bool
    dropped: str | None = None


def record_drop(reason: str, form_id: uuid.UUID | None) -> None:
    DROPS[reason] += 1
    log.info("intake_dropped reason=%s form_id=%s count=%d", reason, form_id, DROPS[reason])


async def read_form(pool: asyncpg.Pool, public_key: str) -> dict[str, Any]:
    """What the hosted page needs to render the form: the field codes, the honeypot name, the agency's label and
    locale, the allowed framing origins and the privacy link (no personal data)."""
    async with intake_tx(pool, public_key) as (ctx, form):
        row = await ctx.conn.fetchrow(FORM_SQL, form.form_definition_id, form.tenant_id)
        schema = json.loads(row["schema"]) if isinstance(row["schema"], str) else dict(row["schema"])
        return {
            **schema,
            "honeypot_field": form.honeypot_field,
            "agency_name": row["display_name"],
            "default_locale": row["default_locale"],
            "allowed_origins": list(form.allowed_origins),
            "privacy_notice_url": form.privacy_notice_url,
        }


def origin_allowed(origin: str | None, settings: Settings, form_origins: tuple[str, ...]) -> bool:
    if not settings.intake_origin_check:
        return True
    if not origin:
        return False
    value = origin.strip().lower().rstrip("/")
    return value in settings.intake_page_origins or value in form_origins


async def submit(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    public_key: str,
    raw: Any,
    *,
    origin: str | None,
    form_quota: Callable[[], bool] | None = None,
    quota_window_s: int = 3600,
) -> Submitted:
    if settings.intake_origin_check and not settings.intake_page_origins:
        raise Unavailable("intake_not_configured")
    async with intake_tx(pool, public_key) as (ctx, form):
        if not origin_allowed(origin, settings, form.allowed_origins):
            record_drop("origin", form.intake_form_id)
            raise Forbidden("origin_not_allowed")
        if stage1.honeypot_filled(raw, form.honeypot_field):
            record_drop("honeypot", form.intake_form_id)
            return Submitted(stored=False, dropped="honeypot")
        try:
            answers = stage1.validate(raw, honeypot_field=form.honeypot_field)
        except stage1.ConsentMissing:
            record_drop("no_consent", form.intake_form_id)
            raise Invalid("consent_required", "tick the box to let the agency contact you") from None
        except stage1.Stage1Invalid as exc:
            record_drop("invalid", form.intake_form_id)
            raise Invalid("invalid_submission", "some answers are not valid", fields=exc.fields) from None
        if form_quota is not None and not form_quota():
            record_drop("form_quota", form.intake_form_id)
            raise RateLimited("form_rate_limited", quota_window_s)
        stored = await store_submission(
            ctx,
            Vault(keys, ctx),
            form.intake_form_id,
            answers,
            idempotency_key=f"page:{answers.client_submission_id}",
            created_by=f"intake_form:{form.intake_form_id}",
        )
        return Submitted(stored=True) if stored else Submitted(stored=False, dropped="duplicate")


async def store_submission(
    ctx: AnyContext,
    vault: Vault,
    intake_form_id: uuid.UUID,
    answers: stage1.Stage1Answers,
    *,
    idempotency_key: str,
    created_by: str,
) -> uuid.UUID | None:
    """Encrypt and store validated answers and queue their ``crm_intake_process`` job in the same transaction.
    None when the idempotency key already holds a submission (nothing new is queued). Also used by the Dev seeder."""
    submission_id = uuid.uuid4()
    blob = await vault.encrypt(
        "form_submission", "payload_enc", submission_id, json.dumps(answers.as_json(), ensure_ascii=False)
    )
    stored = await ctx.conn.fetchval(
        INSERT_SQL,
        submission_id,
        intake_form_id,
        stage1.SCHEMA_KEY,
        blob,
        idempotency_key,
        answers.hidden_answers_dropped,
    )
    if stored is None:
        return None
    await queue.enqueue(
        ctx.conn,
        INTAKE_PROCESS,
        {"tenant_id": str(ctx.tenant_id), "submission_id": str(submission_id)},
        idempotency_key=f"{INTAKE_PROCESS}:{submission_id}",
        max_attempts=MAX_JOB_ATTEMPTS,
        created_by=created_by,
    )
    ctx.audit("intake_received", "allowed", "form_submission", str(submission_id))
    return submission_id
