"""The traveller portal base (C-05 code sign-in, C-06 home, C-07 Stage 3, C-08 sign v0, C-10 payments view; card
CRM-18, AA-946; design v3 M11 base, M12 v0, §2.9). No account: the traveller's own signed link (``client_link``
purpose ``portal``) and then a 6-digit code sent to their own e-mail address. This module is the only one that opens
:func:`app.tenancy.client_link_tx` for the purpose ``portal`` (``tests/unit/test_code_rules.py``), and it never
reaches ``app.travellers.reveal`` (``test_reveal_boundary.py``): nothing here decrypts a value to show it, except the
masked e-mail hint of the traveller's own address.

- :func:`request_code`: a code of purpose ``sign_in`` (or ``sign``, which needs a session: "code asked again at
  signing"), 10 minutes, at most :data:`CODES_PER_HOUR` per access; older pending codes of the purpose expire. The
  code goes out as a message from the agency's mailbox (the trip owner's own, else the agency's; errata S247: never a
  platform sender) in the trip conversation with that traveller, dispatched at once (origin ``system``). **The stored
  message shows the code masked** (:data:`CODE_MASK`): the code itself waits under the vault DEK in
  ``otp_challenge.code_enc`` and the mail sender (:mod:`app.mail.sender`) puts it into the mail at send time and
  clears it, so nobody at the agency can read a code in the Messages tab (review S251: an agent who could read a
  signing code could sign as the traveller). **E-sign v0 limit** (re-review S251, CONTEXT.md): the mail provider
  keeps a Sent copy of the code mail in the sending mailbox, so ``delivery = mailbox`` means "sent from the agency's
  mailbox to the traveller's address", not "only the traveller saw the code". No mailbox -> 409
  ``code_delivery_unavailable`` unless ``PORTAL_OTP_DEV_ECHO`` (Dev) returns the code in the answer; such a code is
  marked ``delivery = dev_echo``. A ``sign`` code is refused (403 ``stage3_locked``) while signing is locked, so it
  never spends a mail or one of the hourly codes.
- :func:`verify_code`: at most :data:`MAX_ATTEMPTS` tries per code; the answer of a wrong code is returned, not raised,
  so the attempt counter commits. A good code opens a portal session of :data:`SESSION_HOURS` hours (the router sets
  it as an httpOnly cookie; only its SHA-256 is stored) and moves the access invited -> active.
- :func:`home`: what the signed-in traveller sees: the agency, the trip dates, their own Stage 3 progress (field codes,
  never values), the waiver to sign, and for the lead the party's progress (counts and roles, no names) and the
  payment schedule. Without a session: the agency name and ``signed_in: false`` only.
- :func:`submit_stage3`: **403 ``stage3_locked`` before the deposit is paid** (card R; the database refuses too,
  CRM32; a ``manual_booking`` override into Booked opens it as well: ``crm.portal_unlocked``), then the F1 identity
  fields written under the vault DEK (write-only: never read back here), Stage 3 time on the access, the trip's
  ``s3`` and ``waiting_on`` refreshed.
- :func:`sign`: e-sign v1 (AA-955, card CRM-25; :mod:`app.esign`): one envelope per traveller with the published
  booking terms, the waiver and, when the traveller declines travel insurance, the insurance declination (R13), each
  pinned by the id and SHA-256 the page showed; the typed full name, the consent tick and a fresh ``sign`` code;
  locked before the deposit like Stage 3. An adult signs their own envelope; the adult lead signs as guardian for each
  minor (under 18 on the start date, O-6); a minor's own access is 403 ``guardian_signs``. The merge fields are filled
  in, the envelope + audit page is sealed with the platform seal (:mod:`app.esign.seal`), and stored on a
  ``crm.signature_event`` row (docs DEK) with the client address and user agent (vault DEK); the booking terms
  acceptance is written too (``crm.terms_acceptance`` source ``envelope``). The signature row keeps the code's
  delivery, the canonical evidence record (no PII) and its SHA-256, the envelope SHA-256 and the seal algorithm, all
  sealed. **Every refused signing code is logged** (``signature_event`` kind ``code_refused``: expired, wrong,
  missing, attempts used up; card R).

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
from app.esign import envelope as env
from app.esign import events as sign_events
from app.esign import signing
from app.esign import terms as esign_terms
from app.esign.seal import Sealer, require_sealer
from app.intake import stage1
from app.intake.public import origin_allowed, record_drop
from app.mail import threads
from app.portal import progress
from app.portal.codes import CODE_MASK
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
UPDATE crm.otp_challenge SET status = 'expired', code_enc = NULL
 WHERE access_id = $1 AND purpose = $2 AND status = 'pending'
"""
INSERT_CODE_SQL = """
INSERT INTO crm.otp_challenge (otp_id, access_id, trip_id, traveler_id, purpose, code_hash, expires_at, delivery)
VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(mins => $7), $8)
"""
CODE_MAIL_SQL = "UPDATE crm.otp_challenge SET message_id = $2, code_enc = $3 WHERE otp_id = $1"
PENDING_CODE_SQL = """
SELECT otp_id, code_hash, attempts, delivery, expires_at <= now() AS expired FROM crm.otp_challenge
 WHERE access_id = $1 AND purpose = $2 AND status = 'pending'
 ORDER BY created_at DESC LIMIT 1
   FOR UPDATE
"""
ATTEMPT_SQL = """
UPDATE crm.otp_challenge SET attempts = attempts + 1,
       status = CASE WHEN attempts + 1 >= $2 THEN 'failed' ELSE status END,
       code_enc = CASE WHEN attempts + 1 >= $2 THEN NULL ELSE code_enc END
 WHERE otp_id = $1
RETURNING attempts, status
"""
EXPIRE_CODE_SQL = "UPDATE crm.otp_challenge SET status = 'expired', code_enc = NULL WHERE otp_id = $1"
SIGNED_IN_SQL = """
UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), session_hash = $2,
       session_expires_at = now() + make_interval(hours => $3), code_enc = NULL
 WHERE otp_id = $1
RETURNING session_expires_at
"""
CONSUME_SQL = """
UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), consumed_at = now(), code_enc = NULL
 WHERE otp_id = $1
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
SELECT request_id, status, signed_at, signer_role FROM crm.signature_request
 WHERE trip_id = $1 AND traveler_id = $2 AND doc_id = $3 AND status <> 'void' AND deleted_at IS NULL
   FOR UPDATE
"""
INSERT_SIGNATURE_SQL = """
INSERT INTO crm.signature_request (request_id, trip_id, traveler_id, access_id, doc_id, signer_traveler_id, signer_role)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
SIGNER_SQL = """
UPDATE crm.signature_request SET access_id = $2, signer_traveler_id = $3, signer_role = $4 WHERE request_id = $1
"""
SIGN_SQL = """
UPDATE crm.signature_request SET status = 'signed', signed_at = $2, signed_name_enc = $3, doc_sha256 = $4,
       evidence_sha256 = $5, otp_id = $6, code_delivery = $7, evidence_record = $8, terms_doc_id = $9,
       terms_sha256 = $10, declination_doc_id = $11, declination_sha256 = $12, insurance_choice = $13,
       envelope_sha256 = $14, seal_alg = $15
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


async def _unlocked(ctx: AnyContext, trip_id: uuid.UUID) -> bool:
    """The portal lock (card R): a paid deposit, or a manual_booking override into Booked (crm.portal_unlocked)."""
    return bool(await ctx.conn.fetchval(stages.PORTAL_UNLOCKED_SQL, trip_id))


async def _require_unlocked(ctx: AnyContext, access: Any, action: str) -> None:
    if not await _unlocked(ctx, access["trip_id"]):
        ctx.audit(action, "denied", "traveler_access", str(access["access_id"]), reason="stage3_locked")
        raise Forbidden("stage3_locked", "Stage 3 opens after the deposit is paid")


async def _deliver(
    ctx: AnyContext, vault: Vault, access: Any, purpose: str, otp_id: uuid.UUID, code: str
) -> str | None:
    """Queue the code mail from the agency's own mailbox; the masked address, or None when no mailbox can send.
    The stored message carries :data:`CODE_MASK`; the code waits sealed on its ``otp_challenge`` row for the sender."""
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
            "message", "body_enc", message_id, body.format(code=CODE_MASK, minutes=CODE_MINUTES), purpose=MAIL_PURPOSE
        ),
    )
    await ctx.conn.execute(
        CODE_MAIL_SQL, otp_id, message_id, await vault.encrypt("otp_challenge", "code_enc", otp_id, code)
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
            await _require_unlocked(ctx, access, "portal_code")
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
            "dev_echo" if settings.portal_otp_dev_echo else "mailbox",
        )
        hint = await _deliver(ctx, Vault(keys, ctx), access, purpose, otp_id, code)  # type: ignore[arg-type]
        if hint is None and not settings.portal_otp_dev_echo:
            raise Conflict("code_delivery_unavailable", "your agent cannot e-mail a code right now; ask them")
        if purpose == "sign":
            await sign_events.log(ctx, "code_sent", access, traveler_id=access["traveler_id"])
        ctx.audit("portal_code", "allowed", "traveler_access", str(access["access_id"]), reason=purpose)
        out: dict[str, Any] = {"sent": hint is not None, "to_hint": hint, "expires_in_s": CODE_MINUTES * 60}
        if settings.portal_otp_dev_echo:
            out["dev_code"] = code
        return out


async def _check_code(ctx: AnyContext, access: Any, purpose: str, code: Any) -> tuple[Any, dict, str | None]:
    """(the verified code's row (otp_id, delivery), {}, None) or (None, the refusal answer, the refusal reason:
    code_missing | code_expired | code_invalid | attempts_exhausted). Never raises on a wrong code, so the attempt
    counter is kept."""
    row = await ctx.conn.fetchrow(PENDING_CODE_SQL, access["access_id"], purpose)
    if row is None:
        return None, {"detail": "code_invalid", "attempts_left": 0}, "code_missing"
    if row["expired"]:
        await ctx.conn.execute(EXPIRE_CODE_SQL, row["otp_id"])
        ctx.audit("portal_code_check", "denied", "traveler_access", str(access["access_id"]), reason="code_expired")
        return None, {"detail": "code_expired", "attempts_left": 0}, "code_expired"
    good = isinstance(code, str) and len(code) == 6 and code.isdigit()
    if good and hmac.compare_digest(code_hash(row["otp_id"], code), bytes(row["code_hash"])):
        return row, {}, None
    after = await ctx.conn.fetchrow(ATTEMPT_SQL, row["otp_id"], MAX_ATTEMPTS)
    ctx.audit("portal_code_check", "denied", "traveler_access", str(access["access_id"]), reason="code_invalid")
    left = max(MAX_ATTEMPTS - int(after["attempts"]), 0)
    return None, {"detail": "code_invalid", "attempts_left": left}, "code_invalid" if left else "attempts_exhausted"


async def verify_code(
    pool: asyncpg.Pool, settings: Settings, token: str, code: Any, *, origin: str | None
) -> tuple[str | None, dict[str, Any]]:
    """(the new session token, the answer); (None, the refusal) for a wrong, expired or missing code."""
    check_origin(settings, origin)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        verified, refusal, _ = await _check_code(ctx, access, "sign_in", code)
        if verified is None:
            return None, refusal
        session = secrets.token_urlsafe(32)
        expires = await ctx.conn.fetchval(SIGNED_IN_SQL, verified["otp_id"], session_hash(session), SESSION_HOURS)
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


async def _agreements_view(ctx: AnyContext, vault: Vault, access: Any, agency: str | None) -> dict[str, Any]:
    found = await signing.published(ctx)
    members = await signing.party(ctx, vault, access["trip_id"], access["start_date"])
    me = next((m for m in members if m.traveler_id == access["traveler_id"]), None)
    values = signing.preview_values(agency, access, access["role"])
    sigs = await signing.signatures(ctx, access["trip_id"], found.waiver.doc_id) if found.waiver else {}
    minor_no = 0
    rows = []
    for m in signing.envelopes(members, access["traveler_id"]):
        sig = sigs.get(m.traveler_id)
        if m.traveler_id != access["traveler_id"]:
            minor_no += 1
        rows.append(
            {
                "traveler_id": str(m.traveler_id),
                "for": "self" if m.traveler_id == access["traveler_id"] else "minor",
                "minor_number": None if m.traveler_id == access["traveler_id"] else minor_no,
                "signed": sig is not None and sig["status"] == "signed",
                "signed_at": _iso(sig["signed_at"]) if sig is not None else None,
                "signer_role": sig["signer_role"] if sig is not None else None,
                "insurance_choice": sig["insurance_choice"] if sig is not None else None,
            }
        )
    return {
        "documents": [signing.document_view(d, values) for d in signing.documents(found, None)],
        "insurance_declination": signing.document_view(found.declination, values) if found.declination else None,
        "envelopes": rows,
        "guardian_signs_for_me": bool(me and me.minor),
    }


async def home(pool: asyncpg.Pool, token: str, session: str | None, keys: KeyProvider | None = None) -> dict[str, Any]:
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
        agreements = (
            await _agreements_view(ctx, Vault(keys, ctx), access, agency)  # type: ignore[arg-type]
            if keys is not None and waiver is not None
            else None
        )
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
                "locked": not await _unlocked(ctx, access["trip_id"]),
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
            # AA-955 (e-sign v1): the envelope documents (merge fields shown with placeholders) and who signs what.
            "agreements": agreements,
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
    """The canonical evidence record of a v0 signature (sorted keys, no spaces); v1: :mod:`app.esign.envelope`."""
    return json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()


def _sign_input(raw: Any) -> tuple[str, uuid.UUID | None, str | None]:
    if not isinstance(raw, dict):
        raise Invalid("invalid_submission", "an object", fields=["body"])
    name = stage1.clean_text(raw.get("full_name"), MAX_NAME) if isinstance(raw.get("full_name"), str) else None
    bad = [f for f, ok in (("full_name", bool(name)), ("agree", raw.get("agree") is True)) if not ok]
    target: uuid.UUID | None = None
    if raw.get("traveler_id") is not None:
        try:
            target = uuid.UUID(str(raw.get("traveler_id")))
        except ValueError:
            bad.append("traveler_id")
    insurance = raw.get("insurance")
    if insurance is not None and insurance not in signing.INSURANCE_CHOICES:
        bad.append("insurance")
    if bad:
        raise Invalid("invalid_submission", "type your full name and tick the box", fields=bad)
    return name, target, insurance  # type: ignore[return-value]


async def sign(
    pool: asyncpg.Pool,
    keys: KeyProvider,
    settings: Settings,
    token: str,
    session: str | None,
    raw: Any,
    *,
    origin: str | None,
    sealer: Sealer | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """(signed, answer). A wrong or expired signing code is an answer (False, refusal), so its attempt counts; the
    refusal is logged (``signature_event`` kind ``code_refused``)."""
    check_origin(settings, origin)
    name, target, insurance = _sign_input(raw)
    async with client_link_tx(pool, hash_hex(token), purpose=PURPOSE) as (ctx, link):
        access = await _access(ctx, link)
        await _session(ctx, access, session)
        await _require_unlocked(ctx, access, "portal_sign")
        seal_with = require_sealer(sealer)
        found = await signing.published(ctx)
        if found.waiver is None:
            raise Conflict("nothing_to_sign", "your agent has no waiver to sign")
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        members = await signing.party(ctx, vault, access["trip_id"], access["start_date"])
        me = next((m for m in members if m.traveler_id == access["traveler_id"]), None)
        if me is not None and me.minor:
            ctx.audit("portal_sign", "denied", "traveler_access", str(access["access_id"]), reason="guardian_signs")
            raise Forbidden("guardian_signs", "the lead traveller signs for you as your guardian")
        traveler_id = target or access["traveler_id"]
        allowed = {m.traveler_id: m for m in signing.envelopes(members, access["traveler_id"])}
        subject = allowed.get(traveler_id)
        if subject is None:
            ctx.audit("portal_sign", "denied", "traveler_access", str(access["access_id"]), reason="not_guardian")
            raise Forbidden("not_guardian", "you can sign for yourself and, as the lead traveller, for a minor")
        guardian = traveler_id != access["traveler_id"]
        if found.declination is not None and insurance is None:
            raise Invalid("invalid_submission", "tell us whether you have travel insurance", fields=["insurance"])
        choice = insurance if found.declination is not None else None
        docs = signing.documents(found, choice)
        shown = signing.shown_hashes(raw)
        if any(shown.get(d.doc_id) != d.body_sha256.hex() for d in docs):
            values = signing.preview_values(await ctx.conn.fetchval(AGENCY_SQL, link.tenant_id), access, subject.role)
            raise Conflict("document_changed", "the documents changed; read them again",
                           documents=[signing.document_view(d, values) for d in docs])  # fmt: skip
        waiver_id = uuid.UUID(found.waiver.doc_id)
        signature = await ctx.conn.fetchrow(SIGNATURE_SQL, access["trip_id"], traveler_id, waiver_id)
        if signature is not None and signature["status"] == "signed":
            return True, {"status": "already_signed", "signed_at": _iso(signature["signed_at"])}
        verified, refusal, reason = await _check_code(ctx, access, "sign", raw.get("code"))
        if verified is None:
            await sign_events.log(ctx, "code_refused", access, traveler_id=traveler_id, reason=reason)
            return False, refusal
        otp_id = verified["otp_id"]
        await ctx.conn.execute(CONSUME_SQL, otp_id)
        signer = (access["traveler_id"], "guardian") if guardian else (None, "self")
        request_id = signature["request_id"] if signature is not None else uuid.uuid4()
        if signature is None:
            await ctx.conn.execute(
                INSERT_SIGNATURE_SQL, request_id, access["trip_id"], traveler_id, access["access_id"], waiver_id,
                *signer,
            )  # fmt: skip
        else:
            await ctx.conn.execute(SIGNER_SQL, request_id, access["access_id"], *signer)
        signed_at = await ctx.conn.fetchval("SELECT now()")
        agency = await ctx.conn.fetchval(AGENCY_SQL, link.tenant_id)
        agent = esign_terms.clean_user_agent(user_agent)
        values = {
            **signing.preview_values(agency, access, subject.role),
            "signer.name": name,
            "signer.role": "guardian of a minor" if guardian else "traveller",
            "signed.date": signed_at.date().isoformat(),
        }
        envelope, evidence, evidence_sha = env.build(
            agency_name=agency or "",
            trip={
                "title": access["title"],
                "start_date": _iso(access["start_date"]),
                "end_date": _iso(access["end_date"]),
            },
            documents=docs,
            values=values,
            audit={
                "request_id": str(request_id),
                "trip_id": str(access["trip_id"]),
                "traveler_id": str(traveler_id),
                "traveller_role": subject.role,
                "access_id": str(access["access_id"]),
                "signer_traveler_id": str(access["traveler_id"]),
                "signer_role": signer[1],
                "insurance_choice": choice,
                "otp_id": str(otp_id),
                # mailbox: sent from the agency's mailbox to the signer's address (the provider keeps a Sent copy its
                # owner can read: the e-sign v0 limit stays); dev_echo: the Dev API answer carried it too.
                "code_delivery": verified["delivery"],
                "signed_at": signed_at.isoformat(),
                "typed_name_sha256": env.sha256(name).hex(),
                "client_ip_sha256": env.optional_sha256(client_ip),
                "user_agent_sha256": env.optional_sha256(agent),
                "consent": {"agree": True},
            },
        )
        envelope_sha = env.sha256(envelope)
        seal = await seal_with.seal(envelope_sha)
        by_kind = {d.kind: d for d in docs}
        terms_doc, declination = by_kind.get("booking_terms"), by_kind.get("insurance_declination")
        await ctx.conn.execute(
            SIGN_SQL,
            request_id,
            signed_at,
            await vault.encrypt("signature_request", "signed_name_enc", request_id, name),
            found.waiver.body_sha256,
            evidence_sha,
            otp_id,
            verified["delivery"],
            evidence,
            uuid.UUID(terms_doc.doc_id) if terms_doc else None,
            terms_doc.body_sha256 if terms_doc else None,
            uuid.UUID(declination.doc_id) if declination else None,
            declination.body_sha256 if declination else None,
            choice,
            envelope_sha,
            seal.alg,
        )
        await sign_events.signed(
            ctx,
            vault,
            request_id=request_id,
            access=access,
            traveler_id=traveler_id,
            evidence=evidence,
            evidence_sha256=evidence_sha,
            envelope=envelope,
            envelope_sha256=envelope_sha,
            seal=seal,
            client_ip=client_ip,
            user_agent=agent,
            at=signed_at,
        )
        if terms_doc is not None:
            await esign_terms.record(
                ctx,
                vault,
                trip_id=access["trip_id"],
                source="envelope",
                doc={"doc_id": uuid.UUID(terms_doc.doc_id), "body_sha256": terms_doc.body_sha256},
                typed_name=name,
                at=signed_at,
                traveler_id=traveler_id,
                link_id=link.link_id,
                request_id=request_id,
                client_ip=client_ip,
                user_agent=agent,
            )
        await ctx.conn.execute(COMMITMENT_SQL, access["trip_id"], f"signature:{request_id}")
        await progress.refresh(ctx, access["trip_id"])
        await stages.refresh_waiting_on(ctx, access["trip_id"])
        ctx.audit("portal_sign", "allowed", "signature_request", str(request_id), reason=signer[1])
        return True, {
            "status": "signed",
            "signed_at": signed_at.isoformat(),
            "traveler_id": str(traveler_id),
            "signer_role": signer[1],
            "envelope_sha256": envelope_sha.hex(),
            "seal_alg": seal.alg,
        }
