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

AA-1035 R2 close-out (crm.ops.v1 1.12, crm-045): the address is ALSO kept sealed under a data key of the CRM
(:func:`keep_address`: a fresh AES-256 key from the KMS data key, context ``{tenant_id: the CIS tenant, purpose:
mail}``, stored wrapped next to the blob), so a send again with ``keep_address`` re-issues to the same address
without anybody typing it (support may only do that). The claim clears it; a revoke must too (the crm-045 guard
refuses a revoke that keeps it; the CRM has no revoke path today); an expiry keeps it, because an expired invitation is
re-issued (the usual send again). A row issued before crm-045 or without a key provider keeps none: 409
``address_not_kept``.

6. **Clean-up read** (:func:`account_links`, S263): the partner-account clean-up (:mod:`app.members.accounts`) reads
   every row bound to an identity, as ``job:crm_partner_accounts`` (crm-039, read only), so it never deletes an account
   a studio invitation still uses.

No agency audit: a studio-only CIS tenant has no CRM agency, and ``crm.audit_event`` belongs to one. Log lines carry
ids and codes only, never the address (not even masked) or the token.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from cryptography.exceptions import InvalidTag
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
from app.members.invitations import Claimed, claim_reason, token_hash_hex
from app.members.mail import Mailer, studio_invitation_message
from app.members.recovery import platform_actor
from app.members.team import new_token, normalise_email
from app.tenancy import (
    ACCOUNT_CLEANUP_ACTOR,
    STUDIO_ACTIVATION_ACTOR,
    studio_invitation_tx,
    studio_platform_tx,
    synthetic_agency,
)
from app.vault.errors import UnwrapDenied
from app.vault.provider import KeyProvider

log = logging.getLogger(__name__)

SEAL_VERSION = 1
SEAL_SALT = b"aa-crm/AA-979/studio-invitation/v1"
LOCALES = ("en", "vi")
ACTIVATION_MAX_ATTEMPTS = 12

FIND_SQL = """
SELECT studio_invitation_id, cis_tenant_id, status, email_kept, email_kept_dek, email_kept_kek_provider,
       email_kept_kek_id
  FROM crm.studio_invitation WHERE cis_invitation_id = $1 FOR UPDATE
"""
INSERT_SQL = """
INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, locale, expires_at,
                                   email_kept, email_kept_dek, email_kept_kek_provider, email_kept_kek_id)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(hours => $6), $7, $8, $9, $10)
RETURNING studio_invitation_id, expires_at
"""
REISSUE_SQL = """
UPDATE crm.studio_invitation
   SET status = 'pending', email_enc = $2, token_hash = $3, locale = $4, issued_at = now(),
       expires_at = now() + make_interval(hours => $5),
       email_kept = $6, email_kept_dek = $7, email_kept_kek_provider = $8, email_kept_kek_id = $9
 WHERE studio_invitation_id = $1
RETURNING studio_invitation_id, expires_at
"""
EXPIRE_SQL = "UPDATE crm.studio_invitation SET status = 'expired' WHERE studio_invitation_id = $1"
# AA-1035 (crm-045): the claim clears the kept address (the invitation is never sent again once opened).
CLAIM_SQL = """
UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $2, cognito_sub = $3, claimed_at = now(),
       email_kept = NULL, email_kept_dek = NULL, email_kept_kek_provider = NULL, email_kept_kek_id = NULL
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

KEPT_VERSION = 1
# The KMS encryption-context purpose of the kept address: the key policy allows a fixed list (AA-CIS-Infra
# crm_dek_purposes); "mail" is the purpose of outbound mail data, which the kept address is (it only ever addresses the
# invitation mail). tenant_id is the CIS tenant (a studio-only tenant has no crm.tenant row, so no vault DEK either).
KEPT_PURPOSE = "mail"
NOT_KEPT = "address_not_kept"


@dataclass(frozen=True)
class KeptAddress:
    """The address sealed under a one-off data key (``blob``: version ‖ nonce ‖ ciphertext ‖ tag, AAD = the CIS
    invitation id) and that key wrapped by the CRM KEK (``dek``), with the wrapper and key id that unwrap it."""

    blob: bytes = field(repr=False)
    dek: bytes = field(repr=False)
    provider: str
    kek_id: str

    def columns(self) -> tuple[bytes, bytes, str, str]:
        return self.blob, self.dek, self.provider, self.kek_id


NO_KEPT: tuple[None, None, None, None] = (None, None, None, None)


def _kept_context(cis_tenant_id: uuid.UUID) -> dict[str, str]:
    return {"tenant_id": str(cis_tenant_id), "purpose": KEPT_PURPOSE}


async def keep_address(
    keys: KeyProvider | None, cis_tenant_id: uuid.UUID, cis_invitation_id: uuid.UUID, address: str
) -> KeptAddress | None:
    """Seal ``address`` under a fresh data key of the CRM KEK; None without a key provider (the invitation then cannot
    be sent again to the same address: a typed one is needed). A KMS failure raises KeyUnavailable (503): the mail is
    not sent, so no invitation is issued that the CRM could not keep."""
    if keys is None:
        return None
    wrapper = keys.wrapper(keys.active_provider)
    plaintext, wrapped, kek_id = await wrapper.generate(_kept_context(cis_tenant_id))
    nonce = os.urandom(12)
    sealed = AESGCM(plaintext).encrypt(nonce, address.encode("utf-8"), cis_invitation_id.bytes)
    return KeptAddress(bytes([KEPT_VERSION]) + nonce + sealed, wrapped, wrapper.provider, kek_id)


async def open_kept(keys: KeyProvider | None, row: Any, cis_tenant_id: uuid.UUID, cis_invitation_id: uuid.UUID) -> str:
    """The address a row kept, or 409 ``address_not_kept`` (no kept address, or one that does not open: a changed
    row). No key provider: 503 (KeyProviderNotConfigured, through require_keys)."""
    if row is None or row["email_kept"] is None:
        raise Conflict(NOT_KEPT, "this invitation kept no address; send it to a typed address")
    from app.vault.provider import require_keys  # noqa: PLC0415 — the vault stays optional for this module's tests

    provider = require_keys(keys)
    blob = bytes(row["email_kept"])
    if len(blob) < 1 + 12 + 16 or blob[0] != KEPT_VERSION:
        raise Conflict(NOT_KEPT, "the kept address cannot be opened; send it to a typed address")
    try:
        dek = await provider.wrapper(row["email_kept_kek_provider"]).unwrap(
            bytes(row["email_kept_dek"]), _kept_context(cis_tenant_id), row["email_kept_kek_id"]
        )
        plain = AESGCM(dek).decrypt(blob[1:13], blob[13:], cis_invitation_id.bytes)
    except (UnwrapDenied, InvalidTag):
        log.warning("crm_studio_invitation_kept_denied cis_invitation_id=%s", cis_invitation_id)
        raise Conflict(NOT_KEPT, "the kept address cannot be opened; send it to a typed address") from None
    return plain.decode("utf-8")


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


def _desk_refused(cis_tenant_id: uuid.UUID, desk_role: str, reason: str) -> None:
    log.warning(
        "crm_studio_invitation_desk_refused cis_tenant_id=%s role=%s reason=%s", cis_tenant_id, desk_role, reason
    )


# AA-1035 (crm.ops.v1 1.12): what a desk caller (cis-customer_success / cis-support) may deliver.
DESK_INVITE_ROLES = frozenset({"viewer", "editor"})
OWNER_NOT_ALLOWED = "owner_invitation_not_allowed"
NOT_INVITED = "invitation_not_invited"


async def check_desk_invitation(
    *,
    cis_tenant_id: uuid.UUID,
    cis_invitation_id: uuid.UUID,
    desk_role: str,
    cis: ActivationConfig | None,
) -> None:
    """The terms on which a desk caller (``app.ops.staff_scope.desk_role`` not None) may issue or re-issue a studio
    invitation, checked here because the desk caller holds the WORKFORCE token CIS relays and can call the CRM
    without CIS (CIS refuses the same before it relays: ``admin_tenant360._require_owner_tier``).

    CIS (``tenant.membership.v1`` getTenantMembers of THIS tenant, M2M) must report an ``invited`` membership with this
    invitation id (so an invitation of another tenant is never delivered under this one) and the role ``viewer`` or
    ``editor``: the owner seat is sent only by cis-admin / crm-platform-admin (tenant.write), never redirected to an
    address a desk caller names or re-sent by one. The role and the status come from CIS, never from the body.

    403 ``invitation_not_invited`` (CIS has no invited membership with this id on this tenant), 403
    ``owner_invitation_not_allowed`` (any other role); 503 ``cis_unavailable`` when CIS cannot answer (fail closed)."""
    if cis is None:
        raise Unavailable("cis_unavailable", "CIS is not configured; the invitation cannot be checked")
    found = await invitation_lookup.invitation(cis, cis_tenant_id, cis_invitation_id)
    if found.kind == "error":
        log.warning("crm_studio_invitation_desk_lookup_failed cis_tenant_id=%s code=%s", cis_tenant_id, found.code)
        raise Unavailable("cis_unavailable", "CIS did not answer; try again in a minute")
    if found.kind == "missing" or found.status != "invited":
        _desk_refused(cis_tenant_id, desk_role, "status")
        raise Forbidden(NOT_INVITED, "CIS holds no open invitation with this id for this tenant")
    if found.role not in DESK_INVITE_ROLES:
        _desk_refused(cis_tenant_id, desk_role, "role")
        raise Forbidden(OWNER_NOT_ALLOWED, "an owner invitation is sent by an administrator only")


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
    email: str | None,
    locale: str,
    staff_sub: str,
    mailer: Mailer,
    web_origin: str,
    ttl_hours: int,
    keys: KeyProvider | None = None,
) -> dict[str, Any]:
    """Create or re-issue the studio invitation of one CIS invitation and send the AA system mail inside the
    transaction (a mail failure rolls the row back: no invitation without its mail).

    AA-1035 (crm.ops.v1 1.12): ``email`` None = send again to the address the row kept (409 ``address_not_kept``
    when it kept none: a row issued before crm-045, without a key provider, or never issued). Every issue with an
    address keeps it (``keys``; without a provider nothing is kept)."""
    if locale not in LOCALES:
        raise Invalid("invalid_locale", "en or vi")
    if not web_origin:
        raise ServiceError("invitation_origin_not_configured", "PARTNER_WEB_ORIGIN is not set")
    address = normalise_email(email) if email is not None else None
    kept = await keep_address(keys, cis_tenant_id, cis_invitation_id, address) if address is not None else None
    token, token_hash = new_token()
    async with studio_platform_tx(pool, actor=platform_actor(staff_sub)) as ctx:
        row = await ctx.conn.fetchrow(FIND_SQL, cis_invitation_id)
        if row is not None:
            if row["cis_tenant_id"] != cis_tenant_id:
                raise Conflict("invitation_tenant_mismatch", "this CIS invitation belongs to another tenant")
            if row["status"] == "claimed":
                raise Conflict("invitation_claimed", "the person opened the invitation; they sign in and accept it")
            if row["status"] in ("accepted", "activated"):
                raise Conflict("invitation_accepted", "the person already accepted this invitation")
            if row["status"] == "revoked":
                raise Gone("invitation_revoked", "this invitation was revoked; CIS creates a new one")
        if address is None:
            address = await open_kept(keys, row, cis_tenant_id, cis_invitation_id)
            kept_columns: tuple[Any, ...] = (
                row["email_kept"],
                row["email_kept_dek"],
                row["email_kept_kek_provider"],
                row["email_kept_kek_id"],
            )
        else:
            kept_columns = kept.columns() if kept is not None else NO_KEPT
        sealed = seal_email(token, cis_invitation_id, address)
        if row is None:
            made = await ctx.conn.fetchrow(
                INSERT_SQL, cis_tenant_id, cis_invitation_id, sealed, token_hash, locale, ttl_hours, *kept_columns
            )
            reissued = False
        else:
            made = await ctx.conn.fetchrow(
                REISSUE_SQL, row["studio_invitation_id"], sealed, token_hash, locale, ttl_hours, *kept_columns
            )
            reissued = True
        await mailer.send(
            studio_invitation_message(address, link=f"{web_origin}/join?t={token}", expires_at=made["expires_at"])
        )
    log.info(
        "crm_studio_invitation_issued studio_invitation_id=%s cis_tenant_id=%s reissued=%s kept=%s to_kept=%s",
        made["studio_invitation_id"],
        cis_tenant_id,
        reissued,
        kept_columns[0] is not None,
        email is None,
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
                # S263 join fix: never a second password for one claimed link (a second tab or a double submit must
                # not replace the one shown); a new invitation re-issues it while the account was never signed in to.
                result = Claimed(state="already_claimed", email=email)
                log.info(
                    "crm_studio_invitation_claimed studio_invitation_id=%s reason=already_claimed",
                    inv.studio_invitation_id,
                )
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


# ── 6. the partner-account clean-up's read (S263, AA-939 follow-up; crm-039) ─────────────────────────────────────────
# Every row that names a partner-pool identity, with what decides whether that account is still needed. crm-039 lets
# ACCOUNT_CLEANUP_ACTOR read (never write) the rows; before crm-039 is applied that actor would see no row at all, so
# the read refuses to run then (fail closed: no studio link must never mean "not read").
CLEANUP_VERSION = "crm-039"
CLEANUP_READY_SQL = "SELECT EXISTS (SELECT 1 FROM crm.v_schema_versions WHERE version = $1) AS ok"
ACCOUNT_LINKS_SQL = """
SELECT studio_invitation_id, cognito_iss, cognito_sub, status, expires_at, updated_at
  FROM crm.studio_invitation
 WHERE cognito_sub IS NOT NULL
"""


@dataclass(frozen=True)
class StudioLink:
    """A Content studio invitation bound to a partner-pool identity (ids, a status and times only)."""

    studio_invitation_id: uuid.UUID
    cognito_iss: str
    cognito_sub: str
    status: str
    expires_at: dt.datetime
    updated_at: dt.datetime


async def account_links(pool: asyncpg.Pool) -> list[StudioLink]:
    """Every studio invitation bound to an identity, read as the clean-up job (:data:`ACCOUNT_CLEANUP_ACTOR`).
    Raises :class:`~app.errors.Unavailable` ``cleanup_not_ready`` while crm-039 is not registered."""
    async with studio_platform_tx(pool, actor=ACCOUNT_CLEANUP_ACTOR) as ctx:
        if not await ctx.conn.fetchval(CLEANUP_READY_SQL, CLEANUP_VERSION):
            raise Unavailable("cleanup_not_ready", f"{CLEANUP_VERSION} is not applied")
        rows = await ctx.conn.fetch(ACCOUNT_LINKS_SQL)
    return [
        StudioLink(
            studio_invitation_id=r["studio_invitation_id"],
            cognito_iss=r["cognito_iss"],
            cognito_sub=r["cognito_sub"],
            status=r["status"],
            expires_at=r["expires_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
