"""The traveller portal base (C-05 code sign-in, C-06 home, C-07 Stage 3, C-08 sign v0, C-10 payments view; card
CRM-18, AA-946; design v3 M11 base, M12 v0, §2.9). No account: the traveller's own signed link (``client_link``
purpose ``portal``) and then a 6-digit code sent to their own e-mail address. This module is the only one that opens
:func:`app.tenancy.client_link_tx` for the purpose ``portal`` (``tests/unit/test_code_rules.py``), and it never
reaches ``app.travellers.reveal`` (``test_reveal_boundary.py``): nothing here decrypts a value to show it, except the
masked e-mail hint of the traveller's own address.

- :func:`request_code`: a code of purpose ``sign_in`` (or ``sign``, which needs a session: "code asked again at
  signing"), 10 minutes, at most :data:`CODES_PER_HOUR` per access; older pending codes of the purpose expire. The
  code goes out as a message from the agency's mailbox (the trip owner's own, else the agency's; errata S247: never a
  platform sender) in the trip conversation with that traveller, dispatched at once (origin ``system``). No mailbox ->
  409 ``code_delivery_unavailable`` unless ``PORTAL_OTP_DEV_ECHO`` (Dev) returns the code in the answer.
- :func:`verify_code`: at most :data:`MAX_ATTEMPTS` tries per code; the answer of a wrong code is returned, not raised,
  so the attempt counter commits. A good code opens a portal session of :data:`SESSION_HOURS` hours (the router sets
  it as an httpOnly cookie; only its SHA-256 is stored) and moves the access invited -> active.
- :func:`home`: what the signed-in traveller sees: the agency, the trip dates, their own Stage 3 progress (field codes,
  never values), the waiver to sign, and for the lead the party's progress (counts and roles, no names) and the
  payment schedule. Without a session: the agency name and ``signed_in: false`` only.
- :func:`submit_stage3`: **403 ``stage3_locked`` before the deposit is paid** (card R; the database refuses too,
  CRM32), then the F1 identity fields written under the vault DEK (write-only: never read back here), Stage 3 time on
  the access, the trip's ``s3`` and ``waiting_on`` refreshed.
- :func:`sign`: e-sign v0: the published waiver version and its SHA-256 as shown, the typed full name, the consent tick
  and a fresh ``sign`` code; locked before the deposit like Stage 3.

Unknown, expired, revoked or foreign link, a link of another purpose or a revoked access: 404 ``link_not_found``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

import asyncpg

from app import stages
from app.dispatch import queue as dispatch
from app.errors import Conflict, Forbidden, Invalid, NotFound, RateLimited, Unavailable
from app.intake import stage1
from app.intake.public import origin_allowed, record_drop
from app.mail import threads
from app.portal import progress
from app.settings import Settings
from app.tenancy import AnyContext, ClientLink, client_link_tx
from app.travellers import age_bands
from app.travellers.fields import clean_f1
from app.vault import KeyProvider, Vault

PURPOSE = "portal"
SESSION_COOKIE = "aa_portal_s"
SESSION_PATH = "/crm/public/v1/portal/"
SESSION_HOURS = 12
CODE_MINUTES = 10
MAX_ATTEMPTS = 5
CODES_PER_HOUR = 5
CODE_PURPOSES = ("sign_in", "sign")
MAIL_PURPOSE = "mail"
MAX_NAME = 200
STAGE3_FIELDS = progress.DETAIL_FIELDS

ACCESS_SQL = """
SELECT a.access_id, a.trip_id, a.traveler_id, a.role, a.status, a.stage3_submitted_at,
       t.stage, t.title, t.start_date, t.end_date, t.owner_member_id
  FROM crm.traveler_access a JOIN crm.trip t ON t.trip_id = a.trip_id
 WHERE a.link_id = $1 AND a.status <> 'revoked' AND a.deleted_at IS NULL
   FOR UPDATE OF a
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
OPENED_SQL = """
UPDATE crm.client_link SET status = 'opened', first_opened_at = now() WHERE link_id = $1 AND status = 'sent'
"""
RECENT_CODES_SQL = """
SELECT count(*) FROM crm.otp_challenge WHERE access_id = $1 AND created_at > now() - interval '1 hour'
"""
EXPIRE_PENDING_SQL = """
UPDATE crm.otp_challenge SET status = 'expired' WHERE access_id = $1 AND purpose = $2 AND status = 'pending'
"""
INSERT_CODE_SQL = """
INSERT INTO crm.otp_challenge (otp_id, access_id, trip_id, traveler_id, purpose, code_hash, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(mins => $7))
"""
PENDING_CODE_SQL = """
SELECT otp_id, code_hash, attempts, expires_at <= now() AS expired FROM crm.otp_challenge
 WHERE access_id = $1 AND purpose = $2 AND status = 'pending'
 ORDER BY created_at DESC LIMIT 1
   FOR UPDATE
"""
ATTEMPT_SQL = """
UPDATE crm.otp_challenge SET attempts = attempts + 1,
       status = CASE WHEN attempts + 1 >= $2 THEN 'failed' ELSE status END
 WHERE otp_id = $1
RETURNING attempts, status
"""
EXPIRE_CODE_SQL = "UPDATE crm.otp_challenge SET status = 'expired' WHERE otp_id = $1"
SIGNED_IN_SQL = """
UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), session_hash = $2,
       session_expires_at = now() + make_interval(hours => $3)
 WHERE otp_id = $1
RETURNING session_expires_at
"""
CONSUME_SQL = """
UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), consumed_at = now() WHERE otp_id = $1
"""
ACTIVATE_SQL = """
UPDATE crm.traveler_access SET last_sign_in_at = now(),
       status = CASE WHEN status = 'invited' THEN 'active' ELSE status END
 WHERE access_id = $1
"""
SESSION_SQL = """
SELECT otp_id FROM crm.otp_challenge
 WHERE access_id = $1 AND session_hash = $2 AND status = 'verified' AND session_expires_at > now()
"""
END_SESSION_SQL = """
UPDATE crm.otp_challenge SET status = 'expired' WHERE access_id = $1 AND session_hash = $2 AND status = 'verified'
"""
EMAIL_SQL = "SELECT email_enc FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
MAILBOX_SQL = """
SELECT channel_account_id FROM crm.channel_account
 WHERE deleted_at IS NULL AND status <> 'disconnected' AND (member_id = $1 OR member_id IS NULL)
 ORDER BY member_id NULLS LAST
 LIMIT 1
"""
INSERT_MESSAGE_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, sender_kind, channel_account_id, subject_enc,
                         body_enc, match_method, occurred_at)
VALUES ($1, $2, 'out', 'queued', 'agency_mailbox', $3, $4, $5, 'compose', now())
"""
TOUCH_CONVERSATION_SQL = "UPDATE crm.conversation SET last_message_at = now() WHERE conversation_id = $1"
STAGE3_SQL = """
UPDATE crm.traveler_access SET stage3_submitted_at = now() WHERE access_id = $1
"""
SCHEDULE_SQL = """
SELECT s.kind, s.amount, s.currency, s.due_on, s.status FROM crm.payment_schedule s
  JOIN crm.proposal p ON p.proposal_id = s.proposal_id AND p.status = 'accepted' AND p.deleted_at IS NULL
 WHERE s.trip_id = $1 AND s.status <> 'void' AND s.deleted_at IS NULL
 ORDER BY s.due_on, s.kind
"""
WAIVER_SQL = """
SELECT doc_id, version_label, body_text, body_sha256 FROM crm.agreement_doc
 WHERE kind = 'waiver' AND status = 'published' AND deleted_at IS NULL
"""
SIGNATURE_SQL = """
SELECT request_id, status, signed_at FROM crm.signature_request
 WHERE trip_id = $1 AND traveler_id = $2 AND doc_id = $3 AND status <> 'void' AND deleted_at IS NULL
   FOR UPDATE
"""
INSERT_SIGNATURE_SQL = """
INSERT INTO crm.signature_request (request_id, trip_id, traveler_id, access_id, doc_id) VALUES ($1, $2, $3, $4, $5)
"""
SIGN_SQL = """
UPDATE crm.signature_request SET status = 'signed', signed_at = $2, signed_name_enc = $3, doc_sha256 = $4,
       evidence_sha256 = $5, otp_id = $6
 WHERE request_id = $1
"""
COMMITMENT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, 'signature', 'signed', $2)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""

CODE_TEXT = {
    "sign_in": (
        "Your sign-in code",
        "Your code to open your trip page is {code}. It is valid for {minutes} minutes.\n\n"
        "If you did not ask for it, you can ignore this e-mail.",
    ),
    "sign": (
        "Your signing code",
        "Your code to sign your travel documents is {code}. It is valid for {minutes} minutes.\n\n"
        "If you did not ask for it, you can ignore this e-mail.",
    ),
}


def hash_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_hash(otp_id: uuid.UUID, code: str) -> bytes:
    return hashlib.sha256(otp_id.bytes + code.encode()).digest()


def session_hash(session: str) -> bytes:
    return hashlib.sha256(session.encode()).digest()


def mask_email(address: str) -> str:
    """``t***@o*****.com``: the first character of the local part and of the domain, the top-level domain."""
    local, _, domain = address.partition("@")
    host, dot, tld = domain.rpartition(".")
    if not host:
        host, dot, tld = domain, "", ""
    return f"{local[:1]}***@{host[:1]}{'*' * max(len(host) - 1, 1)}{dot}{tld}"


def check_origin(settings: Settings, origin: str | None) -> None:
    if settings.intake_origin_check and not settings.intake_page_origins:
        raise Unavailable("intake_not_configured")
    if not origin_allowed(origin, settings, ()):
        record_drop("portal_origin", None)
        raise Forbidden("origin_not_allowed")


async def _access(ctx: AnyContext, link: ClientLink) -> Any:
    row = await ctx.conn.fetchrow(ACCESS_SQL, link.link_id)
    if row is None:
        raise NotFound("link_not_found")
    return row


async def _session(ctx: AnyContext, access: Any, session: str | None) -> uuid.UUID:
    if session and len(session) <= 100:
        found = await ctx.conn.fetchval(SESSION_SQL, access["access_id"], session_hash(session))
        if found is not None:
            return found
    raise Forbidden("portal_sign_in_required", "sign in with the code we e-mail you")


async def _deposit_paid(ctx: AnyContext, trip_id: uuid.UUID) -> bool:
    return bool(await ctx.conn.fetchval(stages.DEPOSIT_PAID_SQL, trip_id))


async def _require_unlocked(ctx: AnyContext, access: Any, action: str) -> None:
    if not await _deposit_paid(ctx, access["trip_id"]):
        ctx.audit(action, "denied", "traveler_access", str(access["access_id"]), reason="stage3_locked")
        raise Forbidden("stage3_locked", "Stage 3 opens after the deposit is paid")


async def _deliver(ctx: AnyContext, vault: Vault, access: Any, purpose: str, code: str) -> str | None:
    """Queue the code mail from the agency's own mailbox; the masked address, or None when no mailbox can send."""
    blob = await ctx.conn.fetchval(EMAIL_SQL, access["traveler_id"])
    if blob is None:
        raise Conflict("no_email_on_file", "your agent has no e-mail address for you")
    address = await vault.decrypt("traveler", "email_enc", access["traveler_id"], blob)
    mailbox = await ctx.conn.fetchval(MAILBOX_SQL, access["owner_member_id"])
    if mailbox is None:
        return None
    conversation = await threads.conversation_for(ctx, access["trip_id"], access["traveler_id"])
    message_id = uuid.uuid4()
    subject, body = CODE_TEXT[purpose]
    await ctx.conn.execute(
        INSERT_MESSAGE_SQL,
        message_id,
        conversation,
        mailbox,
        await vault.encrypt("message", "subject_enc", message_id, subject, purpose=MAIL_PURPOSE),
        await vault.encrypt(
            "message", "body_enc", message_id, body.format(code=code, minutes=CODE_MINUTES), purpose=MAIL_PURPOSE
        ),
    )
    await ctx.conn.execute(TOUCH_CONVERSATION_SQL, conversation)
    await dispatch.enqueue_message(
        ctx,
        traveler_id=access["traveler_id"],
        channel="email",
        purpose="transactional",
        origin="system",
        trip_id=access["trip_id"],
        message_id=message_id,
    )
    return mask_email(address)


async def request_code(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    token: str,
    *,
    purpose: str,
    session: str | None,
    origin: str | None,
) -> dict[str, Any]:
    if purpose not in CODE_PURPOSES:
        raise Invalid("invalid_purpose", ", ".join(CODE_PURPOSES))
    check_origin(settings, origin)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        if purpose == "sign":
            await _session(ctx, access, session)
        if int(await ctx.conn.fetchval(RECENT_CODES_SQL, access["access_id"]) or 0) >= CODES_PER_HOUR:
            raise RateLimited("too_many_codes", 3600, "wait before asking for another code")
        await ctx.conn.execute(EXPIRE_PENDING_SQL, access["access_id"], purpose)
        code = f"{secrets.randbelow(10**6):06d}"
        otp_id = uuid.uuid4()
        await ctx.conn.execute(
            INSERT_CODE_SQL,
            otp_id,
            access["access_id"],
            access["trip_id"],
            access["traveler_id"],
            purpose,
            code_hash(otp_id, code),
            CODE_MINUTES,
        )
        hint = await _deliver(ctx, Vault(keys, ctx), access, purpose, code)  # type: ignore[arg-type]
        if hint is None and not settings.portal_otp_dev_echo:
            raise Conflict("code_delivery_unavailable", "your agent cannot e-mail a code right now; ask them")
        ctx.audit("portal_code", "allowed", "traveler_access", str(access["access_id"]), reason=purpose)
        out: dict[str, Any] = {"sent": hint is not None, "to_hint": hint, "expires_in_s": CODE_MINUTES * 60}
        if settings.portal_otp_dev_echo:
            out["dev_code"] = code
        return out


async def _check_code(ctx: AnyContext, access: Any, purpose: str, code: Any) -> tuple[uuid.UUID | None, dict]:
    """(the verified code's id, {}) or (None, the refusal answer). Never raises on a wrong code, so the attempt
    counter is kept."""
    row = await ctx.conn.fetchrow(PENDING_CODE_SQL, access["access_id"], purpose)
    if row is None:
        return None, {"detail": "code_invalid", "attempts_left": 0}
    if row["expired"]:
        await ctx.conn.execute(EXPIRE_CODE_SQL, row["otp_id"])
        return None, {"detail": "code_expired", "attempts_left": 0}
    good = isinstance(code, str) and len(code) == 6 and code.isdigit()
    if good and hmac.compare_digest(code_hash(row["otp_id"], code), bytes(row["code_hash"])):
        return row["otp_id"], {}
    after = await ctx.conn.fetchrow(ATTEMPT_SQL, row["otp_id"], MAX_ATTEMPTS)
    ctx.audit("portal_code_check", "denied", "traveler_access", str(access["access_id"]), reason="code_invalid")
    return None, {"detail": "code_invalid", "attempts_left": max(MAX_ATTEMPTS - int(after["attempts"]), 0)}


async def verify_code(
    pool: asyncpg.Pool, settings: Settings, token: str, code: Any, *, origin: str | None
) -> tuple[str | None, dict[str, Any]]:
    """(the new session token, the answer); (None, the refusal) for a wrong, expired or missing code."""
    check_origin(settings, origin)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        otp_id, refusal = await _check_code(ctx, access, "sign_in", code)
        if otp_id is None:
            return None, refusal
        session = secrets.token_urlsafe(32)
        expires = await ctx.conn.fetchval(SIGNED_IN_SQL, otp_id, session_hash(session), SESSION_HOURS)
        await ctx.conn.execute(ACTIVATE_SQL, access["access_id"])
        ctx.audit("portal_sign_in", "allowed", "traveler_access", str(access["access_id"]))
        return session, {"signed_in": True, "session_expires_at": expires.isoformat()}


async def sign_out(pool: asyncpg.Pool, token: str, session: str | None) -> dict[str, Any]:
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        if session:
            await ctx.conn.execute(END_SESSION_SQL, access["access_id"], session_hash(session))
        return {"signed_in": False}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def home(pool: asyncpg.Pool, token: str, session: str | None) -> dict[str, Any]:
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        await ctx.conn.execute(OPENED_SQL, link.link_id)
        agency = await ctx.conn.fetchval(AGENCY_SQL, link.tenant_id)
        try:
            await _session(ctx, access, session)
        except Forbidden:
            return {"agency_name": agency, "signed_in": False}
        done = await progress.compute(ctx, access["trip_id"])
        mine = next((t for t in done.travellers if t.traveler_id == access["traveler_id"]), None)
        waiver = await ctx.conn.fetchrow(WAIVER_SQL)
        signature = (
            await ctx.conn.fetchrow(SIGNATURE_SQL, access["trip_id"], access["traveler_id"], waiver["doc_id"])
            if waiver is not None
            else None
        )
        out: dict[str, Any] = {
            "agency_name": agency,
            "signed_in": True,
            "role": access["role"],
            "trip": {
                "title": access["title"],
                "start_date": _iso(access["start_date"]),
                "end_date": _iso(access["end_date"]),
                "stage": access["stage"],
            },
            "stage3": {
                "locked": not await _deposit_paid(ctx, access["trip_id"]),
                "fields": list(STAGE3_FIELDS),
                "missing": list(mine.missing) if mine is not None else list(STAGE3_FIELDS),
                "done": bool(mine and mine.details_done),
                "submitted_at": _iso(access["stage3_submitted_at"]),
            },
            "waiver": None
            if waiver is None
            else {
                "doc_id": str(waiver["doc_id"]),
                "version_label": waiver["version_label"],
                "body_text": waiver["body_text"],
                "body_sha256": bytes(waiver["body_sha256"]).hex(),
                "signed": signature is not None and signature["status"] == "signed",
                "signed_at": _iso(signature["signed_at"]) if signature is not None else None,
            },
        }
        if access["role"] == "lead":
            out["party"] = [
                {"role": t.role, "details_done": t.details_done, "waiver_signed": t.waiver_signed, "me": t is mine}
                for t in done.travellers
            ]
            out["payments"] = [
                {
                    "kind": r["kind"],
                    "amount": str(r["amount"]),
                    "currency": r["currency"],
                    "due_on": r["due_on"].isoformat(),
                    "status": r["status"],
                }
                for r in await ctx.conn.fetch(SCHEDULE_SQL, access["trip_id"])
            ]
        ctx.audit("portal_view", "allowed", "traveler_access", str(access["access_id"]))
        return out


def _stage3_answers(raw: Any) -> dict[str, str | None]:
    if not isinstance(raw, dict) or not raw:
        raise Invalid("invalid_submission", "an object of Stage 3 fields", fields=["body"])
    unknown = sorted(set(raw) - set(STAGE3_FIELDS))
    if unknown:
        raise Invalid("invalid_submission", "unknown fields", fields=unknown[:20])
    out: dict[str, str | None] = {}
    bad: list[str] = []
    for name, value in raw.items():
        try:
            out[name] = clean_f1(name, value)
        except Invalid:
            bad.append(name)
    if bad:
        raise Invalid("invalid_submission", "fields to fix", fields=sorted(bad))
    return out


async def submit_stage3(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    token: str,
    session: str | None,
    raw: Any,
    *,
    origin: str | None,
) -> dict[str, Any]:
    check_origin(settings, origin)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        await _session(ctx, access, session)
        await _require_unlocked(ctx, access, "portal_stage3")
        answers = _stage3_answers(raw)
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        traveler_id = access["traveler_id"]
        sets, args = [], [traveler_id]
        for name, value in answers.items():
            column = f"{name}_enc"
            args.append(await vault.encrypt("traveler", column, traveler_id, value) if value is not None else None)
            sets.append(f"{column} = ${len(args)}")
        await ctx.conn.execute(
            f"UPDATE crm.traveler SET {', '.join(sets)} WHERE traveler_id = $1",  # noqa: S608 — names checked above
            *args,
        )
        if "dob" in answers:
            dob = dt.date.fromisoformat(answers["dob"]) if answers["dob"] else None
            await age_bands.recompute_for_traveler(ctx, vault, traveler_id, dob)  # type: ignore[arg-type]
        await ctx.conn.execute(STAGE3_SQL, access["access_id"])
        done = await progress.refresh(ctx, access["trip_id"])
        await stages.refresh_waiting_on(ctx, access["trip_id"])
        await stages.touch(ctx, access["trip_id"])
        ctx.audit("portal_stage3", "allowed", "traveler", str(traveler_id), fields=tuple(sorted(answers)))
        mine = next((t for t in done.travellers if t.traveler_id == traveler_id), None)
        return {"status": "received", "missing": list(mine.missing) if mine is not None else []}


def evidence_record(**parts: Any) -> bytes:
    """The canonical evidence record of a signature (sorted keys, no spaces), hashed into ``evidence_sha256``."""
    return json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()


async def sign(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    token: str,
    session: str | None,
    raw: Any,
    *,
    origin: str | None,
) -> tuple[bool, dict[str, Any]]:
    """(signed, answer). A wrong or expired signing code is an answer (False, refusal), so its attempt counts."""
    check_origin(settings, origin)
    if not isinstance(raw, dict):
        raise Invalid("invalid_submission", "an object", fields=["body"])
    name = stage1.clean_text(raw.get("full_name"), MAX_NAME) if isinstance(raw.get("full_name"), str) else None
    bad = [f for f, ok in (("full_name", bool(name)), ("agree", raw.get("agree") is True)) if not ok]
    if bad:
        raise Invalid("invalid_submission", "type your full name and tick the box", fields=bad)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        await _session(ctx, access, session)
        await _require_unlocked(ctx, access, "portal_sign")
        waiver = await ctx.conn.fetchrow(WAIVER_SQL)
        if waiver is None:
            raise Conflict("nothing_to_sign", "your agent has no waiver to sign")
        sha = bytes(waiver["body_sha256"])
        if str(raw.get("doc_id")) != str(waiver["doc_id"]) or raw.get("doc_sha256") != sha.hex():
            raise Conflict("document_changed", "the document changed; read it again", doc_id=str(waiver["doc_id"]))
        signature = await ctx.conn.fetchrow(SIGNATURE_SQL, access["trip_id"], access["traveler_id"], waiver["doc_id"])
        if signature is not None and signature["status"] == "signed":
            return True, {"status": "already_signed", "signed_at": _iso(signature["signed_at"])}
        otp_id, refusal = await _check_code(ctx, access, "sign", raw.get("code"))
        if otp_id is None:
            return False, refusal
        await ctx.conn.execute(CONSUME_SQL, otp_id)
        request_id = signature["request_id"] if signature is not None else uuid.uuid4()
        if signature is None:
            await ctx.conn.execute(
                INSERT_SIGNATURE_SQL, request_id, access["trip_id"], access["traveler_id"], access["access_id"],
                waiver["doc_id"],
            )  # fmt: skip
        signed_at = await ctx.conn.fetchval("SELECT now()")
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        evidence = evidence_record(
            request_id=str(request_id),
            trip_id=str(access["trip_id"]),
            traveler_id=str(access["traveler_id"]),
            access_id=str(access["access_id"]),
            doc_id=str(waiver["doc_id"]),
            doc_sha256=sha.hex(),
            otp_id=str(otp_id),
            signed_at=signed_at.isoformat(),
            typed_name_sha256=hashlib.sha256(name.encode()).hexdigest(),  # type: ignore[union-attr]
            consent={"agree": True},
        )
        await ctx.conn.execute(
            SIGN_SQL,
            request_id,
            signed_at,
            await vault.encrypt("signature_request", "signed_name_enc", request_id, name),  # type: ignore[arg-type]
            sha,
            hashlib.sha256(evidence).digest(),
            otp_id,
        )
        await ctx.conn.execute(COMMITMENT_SQL, access["trip_id"], f"signature:{request_id}")
        await progress.refresh(ctx, access["trip_id"])
        await stages.refresh_waiting_on(ctx, access["trip_id"])
        ctx.audit("portal_sign", "allowed", "signature_request", str(request_id))
        return True, {"status": "signed", "signed_at": signed_at.isoformat()}
