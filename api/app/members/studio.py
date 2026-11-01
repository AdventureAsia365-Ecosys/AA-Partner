"""Content studio invitations (AA-979 P2b, root ADR 0010a; crm-020 ``crm.studio_invitation``).

CIS creates the invitation (an ``invited`` Content studio membership with its ``invitation_id`` and the role CIS
decides) and relays the e-mail once to this API; the CRM delivers it, because only the CRM API may create partner-pool
accounts (root ADR 0006b decision 5). The flow, end to end:

1. **Issue** (:func:`issue`, ``POST /crm/ops/v1/tenants/{id}/studio-invitations``, WORKFORCE ``cis-admin`` through the
   CIS staff relay): one row per CIS invitation id, a fresh single-use token, the address sealed under a key derived
   from that token (:func:`seal_email`), the AA system mail with ``<PARTNER_WEB_ORIGIN>/join?t=<token>``. Sending again
   for the same CIS invitation re-issues the same row (new token, new sealed copy, new expiry) while it is pending or
   expired; a claimed one is left alone (the person continues), an accepted / activated one is done.
2. **Claim** (:func:`claim`, the same public ``/crm/public/v1/invitations/claim`` as a CRM invitation: the router tries
   the CRM invitation first, then this table): the token opens the sealed address, the partner-pool account is created
   or found, the row becomes ``claimed`` with that ``(iss, sub)``.
3. The person signs in (managed login, temporary password, own password, authenticator app).
4. **Accept** (:func:`accept`, ``POST /crm/v1/invitations/accept``, partner-pool bearer): the caller's token must carry
   the claimed ``(iss, sub)``; the row becomes ``accepted`` and a ``crm_studio_activation`` job is queued in the same
   transaction; then one activation is tried at once.
5. **Activate** (:func:`activate_once`, the job and the accept fast path): CIS ``activateInvitation`` with the identity
   (``app.cis.activation``); success -> ``activated``; a CIS outage -> the job retries with its backoff; a CIS refusal
   (another identity, a removed membership) -> the code is kept and the job fails permanently (the L1 console shows it).

No agency audit: a studio-only CIS tenant has no CRM agency, and ``crm.audit_event`` belongs to one. Log lines carry
ids and codes only, never the address (not even masked) or the token.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.agencies import SYNTHETIC_ONLY_ENVIRONMENTS
from app.auth import e2e_staff
from app.auth.route_guard import Caller
from app.cis import invitation_lookup
from app.cis.activation import ActivationConfig, Outcome
from app.cis.activation import activate as cis_activate
from app.errors import Conflict, Forbidden, Gone, Invalid, ServiceError, Unavailable
from app.jobs import queue
from app.jobs.kinds import STUDIO_ACTIVATION
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.members.directory import PartnerDirectory
from app.members.invitations import Claimed, claim_reason, reissue_if_never_signed_in, token_hash_hex
from app.members.mail import Mailer, studio_invitation_message
from app.members.recovery import platform_actor
from app.members.team import new_token, normalise_email
from app.tenancy import STUDIO_ACTIVATION_ACTOR, studio_invitation_tx, studio_platform_tx, synthetic_agency

log = logging.getLogger(__name__)

SEAL_VERSION = 1
SEAL_SALT = b"aa-crm/AA-979/studio-invitation/v1"
LOCALES = ("en", "vi")
ACTIVATION_MAX_ATTEMPTS = 12

FIND_SQL = """
SELECT studio_invitation_id, cis_tenant_id, status FROM crm.studio_invitation WHERE cis_invitation_id = $1 FOR UPDATE
"""
INSERT_SQL = """
INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, locale, expires_at)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(hours => $6))
RETURNING studio_invitation_id, expires_at
"""
REISSUE_SQL = """
UPDATE crm.studio_invitation
   SET status = 'pending', email_enc = $2, token_hash = $3, locale = $4, issued_at = now(),
       expires_at = now() + make_interval(hours => $5)
 WHERE studio_invitation_id = $1
RETURNING studio_invitation_id, expires_at
"""
EXPIRE_SQL = "UPDATE crm.studio_invitation SET status = 'expired' WHERE studio_invitation_id = $1"
CLAIM_SQL = """
UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $2, cognito_sub = $3, claimed_at = now()
 WHERE studio_invitation_id = $1 AND status = 'pending'
"""
ACCEPT_SQL = """
UPDATE crm.studio_invitation SET status = 'accepted', accepted_at = now()
 WHERE studio_invitation_id = $1 AND status = 'claimed'
"""
JOB_ROW_SQL = """
SELECT cis_invitation_id, status, cognito_iss, cognito_sub FROM crm.studio_invitation
 WHERE studio_invitation_id = $1 FOR UPDATE
"""
ATTEMPT_SQL = """
UPDATE crm.studio_invitation SET activation_attempts = activation_attempts + 1, activation_error = $2
 WHERE studio_invitation_id = $1 AND status = 'accepted'
"""
ACTIVATED_SQL = """
UPDATE crm.studio_invitation
   SET status = 'activated', activated_at = now(), activation_attempts = activation_attempts + 1,
       activation_error = NULL
 WHERE studio_invitation_id = $1 AND status = 'accepted'
"""


# ── the sealed address ────────────────────────────────────────────────────────────────────────────────────────────


def _key(token: str, cis_invitation_id: uuid.UUID) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=SEAL_SALT, info=cis_invitation_id.bytes).derive(
        token.encode("ascii")
    )


def seal_email(token: str, cis_invitation_id: uuid.UUID, address: str) -> bytes:
    """``version ‖ nonce ‖ ciphertext ‖ tag`` (AES-256-GCM, AAD = the CIS invitation id) under a key only the token
    gives: the database alone cannot open it."""
    nonce = os.urandom(12)
    sealed = AESGCM(_key(token, cis_invitation_id)).encrypt(nonce, address.encode("utf-8"), cis_invitation_id.bytes)
    return bytes([SEAL_VERSION]) + nonce + sealed


def open_email(token: str, cis_invitation_id: uuid.UUID, blob: bytes) -> str:
    if len(blob) < 1 + 12 + 16 or blob[0] != SEAL_VERSION:
        raise Forbidden("decrypt_denied", "the invitation cannot be opened")
    try:
        plain = AESGCM(_key(token, cis_invitation_id)).decrypt(blob[1:13], blob[13:], cis_invitation_id.bytes)
    except Exception:  # noqa: BLE001 — InvalidTag and friends: a wrong token or a changed row
        raise Forbidden("decrypt_denied", "the invitation cannot be opened") from None
    return plain.decode("utf-8")


# ── 0. the Dev-only E2E CIS admin (root ADR 0006a A10b-1) ─────────────────────────────────────────────────────────


def _refuse_e2e(cis_tenant_id: uuid.UUID, reason: str) -> Forbidden:
    log.warning("crm_studio_invitation_e2e_refused cis_tenant_id=%s reason=%s", cis_tenant_id, reason)
    return Forbidden(e2e_staff.E2E_READ_ONLY, "the E2E CIS admin may not send this invitation")


async def check_e2e_invitation(
    pool: asyncpg.Pool,
    *,
    environment: str,
    cis_tenant_id: uuid.UUID,
    cis_invitation_id: uuid.UUID,
    email: str,
    cis: ActivationConfig | None,
) -> None:
    """The terms on which the Dev-only E2E CIS admin (``Caller.e2e_read_only``) may issue or re-issue a studio
    invitation; every other OPS caller skips this. The same terms CIS checks before it relays (AA-CIS-App
    ``admin_tenant360._refuse_e2e_invitation``), checked again here because the CRM is reachable without CIS:

    1. an environment that holds synthetic agencies only (``dev`` / ``test`` / ``local``);
    2. the address is on the E2E mailbox domain (:func:`app.auth.e2e_staff.is_e2e_mailbox_address`);
    3. the CRM holds a synthetic agency with this id (:func:`app.tenancy.synthetic_agency`; no agency = refused);
    4. CIS (``tenant.membership.v1`` getTenantMembers, M2M) has an ``invited`` membership of this tenant with this
       invitation id and the role ``viewer`` or ``editor`` (never the body's word: the body has no role);
    5. in the same answer CIS marks the tenant synthetic (contract 1.2 ``is_synthetic`` exactly ``true``, from
       ``shared.tenants.is_canary``). Term 3 alone is weak on Dev, where the CRM marks every agency synthetic; an
       answer without the field (a 1.1 CIS) or with ``false`` / ``null`` is refused.

    403 ``e2e_read_only`` on any refusal (reason in the log line, never the address); 503 ``cis_unavailable`` when CIS
    cannot answer (fail closed, this caller only)."""
    if environment not in SYNTHETIC_ONLY_ENVIRONMENTS:
        raise _refuse_e2e(cis_tenant_id, "environment")
    if not e2e_staff.is_e2e_mailbox_address(email):
        raise _refuse_e2e(cis_tenant_id, "address")
    if not await synthetic_agency(pool, cis_tenant_id):
        raise _refuse_e2e(cis_tenant_id, "tenant_not_synthetic")
    if cis is None:
        raise Unavailable("cis_unavailable", "CIS is not configured; the invitation cannot be checked")
    found = await invitation_lookup.invitation(cis, cis_tenant_id, cis_invitation_id)
    if found.kind == "error":
        log.warning("crm_studio_invitation_e2e_lookup_failed cis_tenant_id=%s code=%s", cis_tenant_id, found.code)
        raise Unavailable("cis_unavailable", "CIS did not answer; try again in a minute")
    if found.kind == "missing":
        raise _refuse_e2e(cis_tenant_id, "invitation_not_found")
    if found.synthetic is not True:
        raise _refuse_e2e(cis_tenant_id, "cis_synthetic_unknown" if found.synthetic is None else "cis_not_synthetic")
    if found.status != "invited":
        raise _refuse_e2e(cis_tenant_id, "invitation_status")
    if found.role not in e2e_staff.E2E_INVITE_ROLES:
        raise _refuse_e2e(cis_tenant_id, "role")


# ── 1. issue / re-issue (AA staff through the CIS relay) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Issued:
    studio_invitation_id: uuid.UUID
    expires_at: dt.datetime
    reissued: bool


async def issue(
    pool: asyncpg.Pool,
    *,
    cis_tenant_id: uuid.UUID,
    cis_invitation_id: uuid.UUID,
    email: str,
    locale: str,
    staff_sub: str,
    mailer: Mailer,
    web_origin: str,
    ttl_hours: int,
) -> dict[str, Any]:
    """Create or re-issue the studio invitation of one CIS invitation and send the AA system mail inside the
    transaction (a mail failure rolls the row back: no invitation without its mail)."""
    address = normalise_email(email)
    if locale not in LOCALES:
        raise Invalid("invalid_locale", "en or vi")
    if not web_origin:
        raise ServiceError("invitation_origin_not_configured", "PARTNER_WEB_ORIGIN is not set")
    token, token_hash = new_token()
    sealed = seal_email(token, cis_invitation_id, address)
    async with studio_platform_tx(pool, actor=platform_actor(staff_sub)) as ctx:
        row = await ctx.conn.fetchrow(FIND_SQL, cis_invitation_id)
        if row is None:
            made = await ctx.conn.fetchrow(
                INSERT_SQL, cis_tenant_id, cis_invitation_id, sealed, token_hash, locale, ttl_hours
            )
            reissued = False
        else:
            if row["cis_tenant_id"] != cis_tenant_id:
                raise Conflict("invitation_tenant_mismatch", "this CIS invitation belongs to another tenant")
            if row["status"] == "claimed":
                raise Conflict("invitation_claimed", "the person opened the invitation; they sign in and accept it")
            if row["status"] in ("accepted", "activated"):
                raise Conflict("invitation_accepted", "the person already accepted this invitation")
            if row["status"] == "revoked":
                raise Gone("invitation_revoked", "this invitation was revoked; CIS creates a new one")
            made = await ctx.conn.fetchrow(
                REISSUE_SQL, row["studio_invitation_id"], sealed, token_hash, locale, ttl_hours
            )
            reissued = True
        await mailer.send(
            studio_invitation_message(address, link=f"{web_origin}/join?t={token}", expires_at=made["expires_at"])
        )
    log.info(
        "crm_studio_invitation_issued studio_invitation_id=%s cis_tenant_id=%s reissued=%s",
        made["studio_invitation_id"],
        cis_tenant_id,
        reissued,
    )
    return {
        "invitation_id": str(cis_invitation_id),
        "tenant_id": str(cis_tenant_id),
        "status": "pending",
        "expires_at": made["expires_at"].isoformat(),
        "reissued": reissued,
    }


# ── 2. claim (no bearer) ─────────────────────────────────────────────────────────────────────────────────────────


async def claim(pool: asyncpg.Pool, directory: PartnerDirectory, token: str) -> Claimed:
    """The studio side of ``/crm/public/v1/invitations/claim`` (same answers as a CRM invitation's claim)."""
    digest = token_hash_hex(token)
    result: Claimed | None = None
    expired = False
    async with studio_invitation_tx(pool, digest) as (ctx, inv):
        if inv.status in ("accepted", "activated", "revoked"):
            raise Gone("invitation_used")
        if inv.status == "expired":
            raise Gone("invitation_expired")
        if inv.expired:
            # Leave the block normally so the expired mark commits, then answer 410 below.
            await ctx.conn.execute(EXPIRE_SQL, inv.studio_invitation_id)
            expired = True
        else:
            email = open_email(token, inv.cis_invitation_id, inv.email_enc)
            if inv.status == "claimed":
                # S263 join fix: the bound account never signed in -> a new temporary password (as a CRM invitation).
                password = await reissue_if_never_signed_in(directory, inv.cognito_iss, inv.cognito_sub, email)
                if password is not None:
                    result = Claimed(state="created", email=email, temporary_password=password)
                    log.info(
                        "crm_studio_invitation_claimed studio_invitation_id=%s reason=account_password_reissued",
                        inv.studio_invitation_id,
                    )
                else:
                    result = Claimed(state="already_claimed", email=email)
            else:
                account = await directory.create_or_find(email)
                await ctx.conn.execute(CLAIM_SQL, inv.studio_invitation_id, directory.issuer, account.sub)
                result = Claimed(
                    state="created" if account.temporary_password else "existing",
                    email=email,
                    temporary_password=account.temporary_password,
                )
                log.info(
                    "crm_studio_invitation_claimed studio_invitation_id=%s reason=%s",
                    inv.studio_invitation_id,
                    claim_reason(account),
                )
    if expired:
        raise Gone("invitation_expired", "the invitation expired; ask Adventure Asia to send it again")
    assert result is not None  # noqa: S101 — set on every path that reaches here
    return result


# ── 4. accept (partner-pool bearer) ──────────────────────────────────────────────────────────────────────────────

MISMATCH = "sign in with the account this invitation was opened for"


async def accept(pool: asyncpg.Pool, caller: Caller, token: str, activation: ActivationConfig | None) -> dict[str, Any]:
    """The studio side of ``/crm/v1/invitations/accept``: ``{"kind": "studio", "tenant_id": <CIS tenant>, "status":
    accepted | activated}`` (``accepted`` = CIS is being told; the join page says the studio is being set up)."""
    digest = token_hash_hex(token)
    accepted_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    done: dict[str, Any] | None = None
    expired = False
    async with studio_invitation_tx(pool, digest) as (ctx, inv):
        if inv.status in ("revoked", "expired"):
            raise Gone("invitation_used" if inv.status == "revoked" else "invitation_expired")
        if inv.status == "pending" and inv.expired:
            await ctx.conn.execute(EXPIRE_SQL, inv.studio_invitation_id)
            expired = True
        elif inv.status == "pending":
            raise Gone("invitation_not_claimed", "open the invitation link first")
        elif (caller.iss, caller.sub) != (inv.cognito_iss, inv.cognito_sub):
            log.info(
                "crm_studio_invitation_denied studio_invitation_id=%s reason=identity_mismatch",
                inv.studio_invitation_id,
            )
            raise Forbidden("invitation_identity_mismatch", MISMATCH)
        elif inv.status == "activated":
            done = {"kind": "studio", "tenant_id": str(inv.cis_tenant_id), "status": inv.status}
        elif inv.status == "accepted":
            # Accepted, CIS not told yet (an outage, or the job ran out of attempts): the person opening the page
            # again drives one more job (one per UTC hour at most) and one more fast-path attempt.
            slot = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H")
            await queue.enqueue(
                ctx.conn,
                STUDIO_ACTIVATION,
                {"studio_invitation_id": str(inv.studio_invitation_id)},
                idempotency_key=f"{STUDIO_ACTIVATION}:{inv.studio_invitation_id}:{slot}",
                max_attempts=ACTIVATION_MAX_ATTEMPTS,
                created_by="invitation",
            )
            accepted_id, tenant_id = inv.studio_invitation_id, inv.cis_tenant_id
        elif inv.expired:
            await ctx.conn.execute(EXPIRE_SQL, inv.studio_invitation_id)
            expired = True
        else:
            await ctx.conn.execute(ACCEPT_SQL, inv.studio_invitation_id)
            await queue.enqueue(
                ctx.conn,
                STUDIO_ACTIVATION,
                {"studio_invitation_id": str(inv.studio_invitation_id)},
                idempotency_key=f"{STUDIO_ACTIVATION}:{inv.studio_invitation_id}",
                max_attempts=ACTIVATION_MAX_ATTEMPTS,
                created_by="invitation",
            )
            accepted_id, tenant_id = inv.studio_invitation_id, inv.cis_tenant_id
    if expired:
        raise Gone("invitation_expired", "the invitation expired; ask Adventure Asia to send it again")
    if done is not None:
        return done
    assert accepted_id is not None and tenant_id is not None  # noqa: S101
    log.info("crm_studio_invitation_accepted studio_invitation_id=%s", accepted_id)
    status = "accepted"
    if activation is not None:
        try:
            outcome = await activate_once(pool, activation, accepted_id)
            status = "activated" if outcome.kind == "activated" else "accepted"
        except Exception:  # noqa: BLE001 — the queued job retries; the person is never shown a failure for it
            log.warning("crm_studio_activation_fast_path_failed studio_invitation_id=%s", accepted_id)
    return {"kind": "studio", "tenant_id": str(tenant_id), "status": status}


# ── 5. activation (the job and the accept fast path) ─────────────────────────────────────────────────────────────


async def activate_once(pool: asyncpg.Pool, config: ActivationConfig, studio_invitation_id: uuid.UUID) -> Outcome:
    """One CIS activation attempt. The CIS call runs outside any transaction (no row lock held across the network);
    the result is written under a fresh lock, and only while the row is still ``accepted`` (a parallel attempt that
    finished first wins)."""
    async with studio_platform_tx(pool, actor=STUDIO_ACTIVATION_ACTOR) as ctx:
        row = await ctx.conn.fetchrow(JOB_ROW_SQL, studio_invitation_id)
    if row is None:
        return Outcome("refused", "not_found")
    if row["status"] == "activated":
        return Outcome("activated", "ok")
    if row["status"] != "accepted":
        return Outcome("refused", f"status_{row['status']}")
    outcome = await cis_activate(config, row["cis_invitation_id"], row["cognito_iss"], row["cognito_sub"])
    async with studio_platform_tx(pool, actor=STUDIO_ACTIVATION_ACTOR) as ctx:
        current = await ctx.conn.fetchrow(JOB_ROW_SQL, studio_invitation_id)
        if current is None or current["status"] != "accepted":
            return Outcome("activated", "ok") if current and current["status"] == "activated" else outcome
        if outcome.kind == "activated":
            await ctx.conn.execute(ACTIVATED_SQL, studio_invitation_id)
        else:
            await ctx.conn.execute(ATTEMPT_SQL, studio_invitation_id, outcome.code)
    log.info(
        "crm_studio_activation studio_invitation_id=%s outcome=%s code=%s",
        studio_invitation_id,
        outcome.kind,
        outcome.code,
    )
    return outcome


class ActivationRetry(Exception):  # noqa: N818 — reads as an outcome; the worker retries it with backoff
    pass


def build_handler(config: ActivationConfig | None):
    """The ``crm_studio_activation`` job: payload ``{studio_invitation_id}``. Without ``CIS_API_URL`` / ``M2M_CLIENT``
    it is retried (the accepted rows wait), so turning the configuration on later finishes them."""

    async def handler(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            studio_invitation_id = uuid.UUID(str(job.payload.get("studio_invitation_id")))
        except ValueError:
            raise PermanentError("invalid_payload") from None
        if config is None:
            raise ActivationRetry("not_configured")
        outcome = await activate_once(pool, config, studio_invitation_id)
        if outcome.kind == "activated":
            return {"studio_invitation_id": str(studio_invitation_id), "status": "activated"}
        if outcome.kind == "refused":
            raise PermanentError(outcome.code)
        raise ActivationRetry(outcome.code)

    return handler
