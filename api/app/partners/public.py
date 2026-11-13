"""A sales partner's own pages (AA-1071, crm-056; S275 study item crm-29). No account: the partner's signed link
(``crm.sales_partner_link``), the only module that opens :func:`app.tenancy.partner_link_tx`
(``tests/unit/test_code_rules.py``). It never reaches ``app.travellers.reveal`` and never decrypts a traveller's value.

- **Intake** (purpose ``intake``, ``/c/r/<token>`` on the client host): the agency's inquiry form the owner chose for
  the partner (:func:`read_form`, the same answer as the public form) and :func:`submit`: the same checks in the same
  order as :func:`app.intake.public.submit` (origin, honeypot, consent, answers, call slot, the form's hourly quota),
  then the submission is stored with ``sales_partner_id`` (``crm_intake_process`` copies it onto the trip; lead
  source ``sales_partner``) and the link's counters move.
- **View** (purpose ``view``, ``/c/a/<token>``, by the host-only cookie ``aa_partner_link``): :func:`request_code`
  e-mails a 6-digit code (10 minutes, :data:`MAX_ATTEMPTS` tries, :data:`CODES_PER_HOUR` per link) to the partner's
  own contact address from the agency's mailbox (the agency-level mailbox first), the stored mail showing the code
  masked; the code waits sealed on ``crm.sales_partner_otp.code_enc`` until the mail sender puts it in. No mailbox ->
  409 ``code_delivery_unavailable`` unless ``PORTAL_OTP_DEV_ECHO`` (Dev) answers the code (``delivery dev_echo``).
  :func:`verify_code` opens a :data:`SESSION_HOURS` h session (httpOnly cookie, only its SHA-256 stored).
  :func:`home` without a session: the agency's name and ``signed_in: false``; with one: the partner's name, the trips
  it referred (short reference, stage group, start month, travellers; never a traveller's name or contact) and the
  commission of each (amount, currency, status, due and paid dates) with the totals.

Unknown, expired, revoked or foreign link, a link of another purpose, an inactive partner: 404 ``link_not_found``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import asyncpg

from app import platform_settings as ps
from app.dispatch import queue as dispatch
from app.errors import Conflict, Forbidden, Invalid, RateLimited, Unavailable
from app.intake import slots, stage1
from app.intake.public import FORM_SQL, origin_allowed, record_drop, store_submission
from app.partners.service import money, trip_ref
from app.portal.codes import CODE_MASK
from app.portal.public import mask_email
from app.settings import Settings
from app.tenancy import AnyContext, PartnerLink, partner_link_tx
from app.vault import KeyProvider, Vault

SESSION_COOKIE = "aa_partner_s"
LINK_COOKIE = "aa_partner_link"
VIEW_PATH = "/crm/public/v1/partner"
SESSION_HOURS = 12
CODE_MINUTES = 10
MAX_ATTEMPTS = 5
CODES_PER_HOUR = 5
MAIL_PURPOSE = "mail"
MAX_TRIPS = 200
# The stage a partner sees: the agency's pipeline in four groups (the inner stages are the agency's business).
STAGE_GROUPS = {
    "inquiry": "enquiry",
    "qualifying": "enquiry",
    "proposal": "quoted",
    "booked": "booked",
    "pre_departure": "booked",
    "travelling": "travelling",
    "completed": "completed",
    "lost": "closed",
    "cancelled": "closed",
}

INTAKE_FORM_SQL = """
SELECT intake_form_id, form_definition_id, allowed_origins, honeypot_field, privacy_notice_url
  FROM crm.intake_form WHERE intake_form_id = $1 AND status = 'active' AND deleted_at IS NULL
"""
USED_SQL = "UPDATE crm.sales_partner_link SET use_count = use_count + 1, last_used_at = now() WHERE link_id = $1"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
PARTNER_SQL = "SELECT name_enc, contact_email_enc FROM crm.sales_partner WHERE sales_partner_id = $1"
RECENT_CODES_SQL = (
    "SELECT count(*) FROM crm.sales_partner_otp WHERE link_id = $1 AND created_at > now() - interval '1 hour'"
)
EXPIRE_PENDING_SQL = """
UPDATE crm.sales_partner_otp SET status = 'expired', code_enc = NULL WHERE link_id = $1 AND status = 'pending'
"""
INSERT_CODE_SQL = """
INSERT INTO crm.sales_partner_otp (otp_id, sales_partner_id, link_id, code_hash, delivery, expires_at)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(mins => $6))
"""
CODE_MAIL_SQL = "UPDATE crm.sales_partner_otp SET message_id = $2, code_enc = $3 WHERE otp_id = $1"
PENDING_CODE_SQL = """
SELECT otp_id, code_hash, expires_at <= now() AS expired FROM crm.sales_partner_otp
 WHERE link_id = $1 AND status = 'pending'
 ORDER BY created_at DESC LIMIT 1
   FOR UPDATE
"""
ATTEMPT_SQL = """
UPDATE crm.sales_partner_otp SET attempts = attempts + 1,
       status = CASE WHEN attempts + 1 >= $2 THEN 'failed' ELSE status END,
       code_enc = CASE WHEN attempts + 1 >= $2 THEN NULL ELSE code_enc END
 WHERE otp_id = $1
RETURNING attempts
"""
EXPIRE_CODE_SQL = "UPDATE crm.sales_partner_otp SET status = 'expired', code_enc = NULL WHERE otp_id = $1"
SIGNED_IN_SQL = """
UPDATE crm.sales_partner_otp SET status = 'verified', verified_at = now(), session_hash = $2,
       session_expires_at = now() + make_interval(hours => $3), code_enc = NULL
 WHERE otp_id = $1
"""
SESSION_SQL = """
SELECT otp_id FROM crm.sales_partner_otp
 WHERE link_id = $1 AND session_hash = $2 AND status = 'verified' AND session_expires_at > now()
"""
END_SESSION_SQL = """
UPDATE crm.sales_partner_otp SET status = 'expired' WHERE link_id = $1 AND session_hash = $2 AND status = 'verified'
"""
MAILBOX_SQL = """
SELECT channel_account_id FROM crm.channel_account
 WHERE deleted_at IS NULL AND status <> 'disconnected'
 ORDER BY member_id NULLS FIRST, created_at, channel_account_id
 LIMIT 1
"""
CONVERSATION_SQL = """
SELECT conversation_id FROM crm.conversation WHERE sales_partner_id = $1 AND deleted_at IS NULL
"""
INSERT_CONVERSATION_SQL = """
INSERT INTO crm.conversation (conversation_id, channel, sales_partner_id) VALUES ($1, 'email', $2)
"""
INSERT_MESSAGE_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, sender_kind, channel_account_id, subject_enc,
                         body_enc, match_method, occurred_at)
VALUES ($1, $2, 'out', 'queued', 'agency_mailbox', $3, $4, $5, 'compose', now())
"""
TOUCH_CONVERSATION_SQL = "UPDATE crm.conversation SET last_message_at = now() WHERE conversation_id = $1"
TRIPS_SQL = """
SELECT t.trip_id, t.stage, t.start_date, t.month, t.party_adults, cardinality(t.party_child_ages) AS children,
       t.created_at,
       c.amount, c.currency, c.status AS commission_status, c.due_on, c.paid_on, c.amount_base, c.base_currency
  FROM crm.trip t
  LEFT JOIN LATERAL (
        SELECT pc.amount, pc.currency, pc.status, pc.due_on, pc.paid_on, pc.amount_base, pc.base_currency
          FROM crm.partner_commission pc
         WHERE pc.trip_id = t.trip_id AND pc.sales_partner_id = t.sales_partner_id AND pc.deleted_at IS NULL
         ORDER BY (pc.status = 'cancelled'), pc.created_at DESC
         LIMIT 1
       ) c ON true
 WHERE t.sales_partner_id = $1 AND t.deleted_at IS NULL
 ORDER BY t.created_at DESC, t.trip_id
 LIMIT $2
"""

CODE_TEXT = (
    "Your sign-in code",
    "Your code to open your partner page is {code}. It is valid for {minutes} minutes.\n\n"
    "If you did not ask for it, you can ignore this e-mail.",
)


def hash_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_hash(otp_id: uuid.UUID, code: str) -> bytes:
    return hashlib.sha256(otp_id.bytes + code.encode()).digest()


def session_hash(session: str) -> bytes:
    return hashlib.sha256(session.encode()).digest()


def check_origin(settings: Settings, origin: str | None) -> None:
    if not origin_allowed(origin, settings, ()):
        raise Forbidden("origin_not_allowed")


# ── intake ───────────────────────────────────────────────────────────────────────────────────────


async def _form(ctx: AnyContext, link: PartnerLink) -> Any:
    form = await ctx.conn.fetchrow(INTAKE_FORM_SQL, link.intake_form_id)
    if form is None:  # the owner disabled or trashed the form the link uses
        raise Unavailable("form_unavailable")
    return form


async def read_form(pool: asyncpg.Pool, token: str) -> dict[str, Any]:
    """What the partner's intake page renders: the same answer as the public form (no client data)."""
    async with partner_link_tx(pool, hash_hex(token), purpose="intake") as (ctx, link):
        form = await _form(ctx, link)
        row = await ctx.conn.fetchrow(FORM_SQL, form["form_definition_id"], link.tenant_id)
        schema = json.loads(row["schema"]) if isinstance(row["schema"], str) else dict(row["schema"])
        return {
            **schema,
            "honeypot_field": form["honeypot_field"],
            "agency_name": row["display_name"],
            "default_locale": row["default_locale"],
            "allowed_origins": list(form["allowed_origins"]),
            "privacy_notice_url": form["privacy_notice_url"] or row["agency_privacy_notice_url"],
            "consent_text": row["consent_text"],
            "data_request_contact": row["data_request_contact"],
            **await slots.offered(ctx),
        }


async def submit(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    token: str,
    raw: Any,
    *,
    origin: str | None,
    form_quota: Callable[..., bool] | None = None,
    quota_window_s: int = 3600,
) -> bool:
    """True when stored (False: a repeat or a honeypot hit, answered the same 202). The checks of the public form."""
    if settings.intake_origin_check and not settings.intake_page_origins:
        raise Unavailable("intake_not_configured")
    async with partner_link_tx(pool, hash_hex(token), purpose="intake") as (ctx, link):
        form = await _form(ctx, link)
        form_id = form["intake_form_id"]
        if not origin_allowed(origin, settings, tuple(form["allowed_origins"])):
            record_drop("origin", form_id)
            raise Forbidden("origin_not_allowed")
        if stage1.honeypot_filled(raw, form["honeypot_field"]):
            record_drop("honeypot", form_id)
            return False
        try:
            answers = stage1.validate(raw, honeypot_field=form["honeypot_field"])
        except stage1.ConsentMissing:
            record_drop("no_consent", form_id)
            raise Invalid("consent_required", "tick the box to let the agency contact you") from None
        except stage1.Stage1Invalid as exc:
            record_drop("invalid", form_id)
            raise Invalid("invalid_submission", "some answers are not valid", fields=exc.fields) from None
        if answers.call_slot is not None and not await slots.is_offered(ctx, answers.call_slot):
            record_drop("invalid", form_id)
            raise Invalid("invalid_submission", "the call slot is no longer free", fields=["call_slot"])
        # AA-1096 L4: the agency's own quota (crm.rate.intake_form_per_hour) or the platform's.
        if form_quota is not None and not form_quota(
            form_id, limit=await ps.tenant_get(ctx.conn, ps.RATE_INTAKE_FORM, ctx.tenant_id)
        ):
            record_drop("form_quota", form_id)
            raise RateLimited("form_rate_limited", quota_window_s)
        stored = await store_submission(
            ctx,
            Vault(keys, ctx),
            form_id,
            answers,
            idempotency_key=f"partner:{link.link_id.hex[:12]}:{answers.client_submission_id}",
            created_by=ctx.actor,
            sales_partner_id=link.sales_partner_id,
        )
        if stored is not None:
            await ctx.conn.execute(USED_SQL, link.link_id)
        return stored is not None


# ── view ─────────────────────────────────────────────────────────────────────────────────────────


async def _session(ctx: AnyContext, link: PartnerLink, session: str | None) -> bool:
    if not session:
        return False
    return await ctx.conn.fetchval(SESSION_SQL, link.link_id, session_hash(session)) is not None


async def _deliver(ctx: AnyContext, vault: Vault, link: PartnerLink, otp_id: uuid.UUID, code: str) -> str | None:
    """Queue the code mail from the agency's mailbox; the masked address, or None when no mailbox can send."""
    row = await ctx.conn.fetchrow(PARTNER_SQL, link.sales_partner_id)
    if row is None or row["contact_email_enc"] is None:
        raise Conflict("no_email_on_file", "the agency has no e-mail address for you")
    address = await vault.decrypt("sales_partner", "contact_email_enc", link.sales_partner_id, row["contact_email_enc"])
    mailbox = await ctx.conn.fetchval(MAILBOX_SQL)
    if mailbox is None:
        return None
    conversation = await ctx.conn.fetchval(CONVERSATION_SQL, link.sales_partner_id)
    if conversation is None:
        conversation = uuid.uuid4()
        await ctx.conn.execute(INSERT_CONVERSATION_SQL, conversation, link.sales_partner_id)
    message_id = uuid.uuid4()
    subject, body = CODE_TEXT
    await ctx.conn.execute(
        INSERT_MESSAGE_SQL,
        message_id,
        conversation,
        mailbox,
        await vault.encrypt("message", "subject_enc", message_id, subject, purpose=MAIL_PURPOSE),
        await vault.encrypt(
            "message", "body_enc", message_id, body.format(code=CODE_MASK, minutes=CODE_MINUTES), purpose=MAIL_PURPOSE
        ),
    )
    await ctx.conn.execute(
        CODE_MAIL_SQL, otp_id, message_id, await vault.encrypt("sales_partner_otp", "code_enc", otp_id, code)
    )
    await ctx.conn.execute(TOUCH_CONVERSATION_SQL, conversation)
    await dispatch.enqueue_partner_message(ctx, sales_partner_id=link.sales_partner_id, message_id=message_id)
    return mask_email(address)


async def request_code(
    pool: asyncpg.Pool, keys: KeyProvider, settings: Settings, token: str, *, origin: str | None
) -> dict[str, Any]:
    check_origin(settings, origin)
    async with partner_link_tx(pool, hash_hex(token), purpose="view") as (ctx, link):
        if int(await ctx.conn.fetchval(RECENT_CODES_SQL, link.link_id) or 0) >= CODES_PER_HOUR:
            raise RateLimited("too_many_codes", 3600, "wait before asking for another code")
        await ctx.conn.execute(EXPIRE_PENDING_SQL, link.link_id)
        code = f"{secrets.randbelow(10**6):06d}"
        otp_id = uuid.uuid4()
        echo = settings.portal_otp_dev_echo
        await ctx.conn.execute(
            INSERT_CODE_SQL,
            otp_id,
            link.sales_partner_id,
            link.link_id,
            code_hash(otp_id, code),
            "dev_echo" if echo else "mailbox",
            CODE_MINUTES,
        )
        hint = await _deliver(ctx, Vault(keys, ctx), link, otp_id, code)
        if hint is None and not echo:
            raise Conflict("code_delivery_unavailable", "the agency cannot e-mail a code right now; ask them")
        ctx.audit("partner_code", "allowed", "sales_partner_link", str(link.link_id))
        out: dict[str, Any] = {"sent": hint is not None, "to_hint": hint, "expires_in_s": CODE_MINUTES * 60}
        if echo:
            out["dev_code"] = code
        return out


async def verify_code(
    pool: asyncpg.Pool, settings: Settings, token: str, code: Any, *, origin: str | None
) -> tuple[str | None, dict[str, Any]]:
    """(the new session, the answer) or (None, the refusal answer: code_invalid / code_expired, attempts_left)."""
    check_origin(settings, origin)
    async with partner_link_tx(pool, hash_hex(token), purpose="view") as (ctx, link):
        row = await ctx.conn.fetchrow(PENDING_CODE_SQL, link.link_id)
        if row is None:
            return None, {"detail": "code_invalid", "attempts_left": 0}
        if row["expired"]:
            await ctx.conn.execute(EXPIRE_CODE_SQL, row["otp_id"])
            ctx.audit("partner_code_check", "denied", "sales_partner_link", str(link.link_id), reason="code_expired")
            return None, {"detail": "code_expired", "attempts_left": 0}
        good = isinstance(code, str) and len(code) == 6 and code.isdigit()
        if not (good and hmac.compare_digest(code_hash(row["otp_id"], code), bytes(row["code_hash"]))):
            attempts = await ctx.conn.fetchval(ATTEMPT_SQL, row["otp_id"], MAX_ATTEMPTS)
            ctx.audit("partner_code_check", "denied", "sales_partner_link", str(link.link_id), reason="code_invalid")
            return None, {"detail": "code_invalid", "attempts_left": max(MAX_ATTEMPTS - int(attempts), 0)}
        session = secrets.token_urlsafe(32)
        await ctx.conn.execute(SIGNED_IN_SQL, row["otp_id"], session_hash(session), SESSION_HOURS)
        await ctx.conn.execute(USED_SQL, link.link_id)
        ctx.audit("partner_sign_in", "allowed", "sales_partner_link", str(link.link_id))
        return session, {"signed_in": True}


async def sign_out(pool: asyncpg.Pool, token: str, session: str | None) -> dict[str, Any]:
    async with partner_link_tx(pool, hash_hex(token), purpose="view") as (ctx, link):
        if session:
            await ctx.conn.execute(END_SESSION_SQL, link.link_id, session_hash(session))
        return {"signed_in": False}


def _trip_view(row: Any) -> dict[str, Any]:
    start = row["start_date"] or row["month"]
    commission = None
    if row["commission_status"] is not None:
        commission = {
            "amount": money(row["amount"]),
            "currency": row["currency"],
            "status": row["commission_status"],
            "due_on": row["due_on"].isoformat() if row["due_on"] else None,
            "paid_on": row["paid_on"].isoformat() if row["paid_on"] else None,
        }
    return {
        "ref": trip_ref(row["trip_id"]),
        "stage": STAGE_GROUPS.get(row["stage"], "enquiry"),
        "start_month": start.strftime("%Y-%m") if isinstance(start, dt.date) else None,
        "travellers": int(row["party_adults"]) + int(row["children"]),
        "received_on": row["created_at"].date().isoformat(),
        "commission": commission,
    }


def _totals(rows: list[Any]) -> list[dict[str, Any]]:
    """Per currency: what is still to come (accrued, approved) and what was paid."""
    sums: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        if r["commission_status"] in (None, "cancelled"):
            continue
        bucket = sums.setdefault(r["currency"], {"open": Decimal(0), "paid": Decimal(0)})
        bucket["paid" if r["commission_status"] == "paid" else "open"] += r["amount"]
    return [{"currency": c, "open": money(v["open"]), "paid": money(v["paid"])} for c, v in sorted(sums.items())]


async def home(pool: asyncpg.Pool, keys: KeyProvider | None, token: str, session: str | None) -> dict[str, Any]:
    async with partner_link_tx(pool, hash_hex(token), purpose="view") as (ctx, link):
        out: dict[str, Any] = {
            "agency": await ctx.conn.fetchval(AGENCY_SQL, link.tenant_id),
            "signed_in": False,
            "link_expires_at": link.expires_at.isoformat(),
        }
        if not await _session(ctx, link, session):
            return out
        row = await ctx.conn.fetchrow(PARTNER_SQL, link.sales_partner_id)
        name = (
            await Vault(keys, ctx).decrypt("sales_partner", "name_enc", link.sales_partner_id, row["name_enc"])
            if keys is not None and row is not None
            else None
        )
        rows = list(await ctx.conn.fetch(TRIPS_SQL, link.sales_partner_id, MAX_TRIPS))
        ctx.audit("partner_view", "allowed", "sales_partner_link", str(link.link_id))
        return {
            **out,
            "signed_in": True,
            "partner_name": name,
            "trips": [_trip_view(r) for r in rows],
            "totals": _totals(rows),
        }
