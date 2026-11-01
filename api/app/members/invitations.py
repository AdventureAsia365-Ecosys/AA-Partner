"""Invitation claim and accept (root ADR 0006b decision 5; AA-939, card CRM-11 "invite -> active < 5 min").

The flow, end to end:

1. The owner invites an address (:func:`app.members.team.invite`): a row with the token's SHA-256, the AA system mail
   with ``/join?t=<token>``.
2. **Claim** (:func:`claim`, ``POST /crm/public/v1/invitations/claim``, no bearer: the person has no account yet).
   The token finds the invitation (:func:`app.tenancy.invitation_tx`); it must be pending and not expired. The address
   is opened by the vault and the partner pool account is **created** (``AdminCreateUser``, ``SUPPRESS``, a random
   temporary password the page shows once) or **found** (the address already has an account). The invitation becomes
   ``claimed`` and keeps that account's ``(iss, sub)``. An existing account that never finished its first sign-in
   (``FORCE_CHANGE_PASSWORD``: a password nobody saw, an earlier invitation that was withdrawn or expired) gets a new
   temporary password (``created``; S263 join fix, see :mod:`app.members.directory`); an account with its own password
   is ``existing`` and is never reset. Calling claim again on a claimed invitation answers ``already_claimed`` with no
   password and changes nothing (a ``denied`` event, reason ``already_claimed``): a second claim must never replace a
   password the first one is showing (a double submit, a second tab). A person who never saw the password asks for a
   new invitation (the owner revokes and re-invites; AA withdraws a first-owner invitation in the L1 console), whose
   claim gives the never-signed-in account a new temporary password.
3. The person signs in through managed login: the temporary password, then their own password, then an authenticator
   app (the pool requires MFA, so no membership is ever active without a second factor).
4. **Accept** (:func:`accept`, ``POST /crm/v1/invitations/accept``, partner-pool bearer): the invitation must be
   claimed and not expired, and the caller's token must carry exactly the claimed ``(iss, sub)`` — the binding is the
   verified token, never an e-mail lookup. Otherwise 403 ``invitation_identity_mismatch`` with a ``denied`` event.
   The membership is created (or a disabled one comes back with the invited role) and the invitation is ``accepted``.
   A person who is already an active member of the agency gets 409 ``already_member`` (an invitation never changes
   an existing role: the owner does that on the Team page), and the crm-008 guards' refusals are 409 too.

``invitation_tx`` locks the invitation row, so two claims (or two accepts) of one invitation run one after the other:
the second sees ``claimed`` (no second account, no second password) or ``accepted`` (410 ``invitation_used``).

The token is single-use in effect: after accept nothing can be done with it; while claimed, only the account it was
bound to can finish. Expired invitations answer 410 ``invitation_expired`` (and are marked expired).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden, NotFound, ServiceError
from app.members.directory import Account, PartnerDirectory
from app.tenancy import SystemContext, invitation_tx
from app.vault import KeyProvider, Vault

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")

EXPIRE_ONE_SQL = "UPDATE crm.member_invitation SET status = 'expired' WHERE invitation_id = $1"
CLAIM_SQL = """
UPDATE crm.member_invitation SET status = 'claimed', cognito_iss = $2, cognito_sub = $3, claimed_at = now()
 WHERE invitation_id = $1 AND status = 'pending'
"""
MEMBER_BY_IDENTITY_SQL = """
SELECT member_id, status FROM crm.member
 WHERE cognito_iss = $1 AND cognito_sub = $2 AND deleted_at IS NULL FOR UPDATE
"""
MEMBER_INSERT_SQL = """
INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active') RETURNING member_id
"""
MEMBER_REACTIVATE_SQL = "UPDATE crm.member SET role = $2, status = 'active' WHERE member_id = $1"
# AA-941 (design S-01 first-owner rule, re-checked at accept): an owner invitation AA's L1 console sent (invited_by
# platform:*) only becomes a membership while the agency still has no active owner and holds no traveller (any
# traveller row, trashed included, or any stored inquiry; the same rule as app.ops.console's invite check). Otherwise a
# new person could become owner of client data without the Q1 6.3 owner recovery.
PLATFORM_OWNER_RECHECK_SQL = """
SELECT i.invited_by LIKE 'platform:%' AS by_platform,
       EXISTS (SELECT 1 FROM crm.member m
                WHERE m.role = 'owner' AND m.status = 'active' AND m.deleted_at IS NULL) AS has_owner,
       (EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS (SELECT 1 FROM crm.form_submission)) AS holds_travellers
  FROM crm.member_invitation i
 WHERE i.invitation_id = $1
"""
ACCEPT_SQL = """
UPDATE crm.member_invitation SET status = 'accepted', accepted_at = now(), member_id = $2
 WHERE invitation_id = $1 AND status = 'claimed'
"""


class Gone(ServiceError):
    status_code = 410


def _accept_conflict(exc: asyncpg.PostgresError) -> Exception:
    """A refusal by the database while accepting -> 409 (anything else goes on unchanged, a 500)."""
    if isinstance(exc, asyncpg.UniqueViolationError):
        return Conflict("invitation_race", "this membership was just created; reload the page")
    if exc.sqlstate == "CRM01":
        return Conflict("last_owner", "an agency keeps at least one active owner")
    if exc.sqlstate == "CRM02":
        return Conflict("role_write_forbidden", "this invitation cannot change that membership")
    return exc


@dataclass(frozen=True)
class Claimed:
    state: str  # created | existing | already_claimed
    email: str
    temporary_password: str | None = None

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "email": self.email}
        if self.temporary_password is not None:
            out["temporary_password"] = self.temporary_password
        return out


def token_hash_hex(token: str) -> str:
    """The SHA-256 (hex) of a well-formed token; a malformed token is 404 like an unknown one."""
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise NotFound("invitation_not_found")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def _refuse_if_closed(ctx: SystemContext, invitation: Any, *, accepting: bool) -> None:
    if invitation.status in {"revoked", "accepted", "expired"}:
        raise Gone("invitation_used" if invitation.status != "expired" else "invitation_expired")
    if invitation.expired:
        await ctx.conn.execute(EXPIRE_ONE_SQL, invitation.invitation_id)
        raise _CommitThen(Gone("invitation_expired", "the invitation expired; ask the agency owner for a new one"))
    if accepting and invitation.status != "claimed":
        raise Gone("invitation_not_claimed", "open the invitation link first")


def claim_reason(account: Account) -> str:
    """The audit reason of a first claim: a new account, an unfinished one with a new password, or an existing one."""
    if account.reissued:
        return "account_password_reissued"
    return "account_created" if account.temporary_password else "account_existing"


class _CommitThen(Exception):  # noqa: N818 — internal control flow: commit the work done, then raise ``error``
    def __init__(self, error: ServiceError) -> None:
        super().__init__(error.code)
        self.error = error


async def claim(pool: asyncpg.Pool, keys: KeyProvider, directory: PartnerDirectory, token: str) -> Claimed:
    digest = token_hash_hex(token)
    result: Claimed | None = None
    error: ServiceError | None = None
    async with invitation_tx(pool, digest) as (ctx, invitation):
        try:
            await _refuse_if_closed(ctx, invitation, accepting=False)
        except _CommitThen as wrapped:
            error = wrapped.error  # leave the context normally: the "expired" mark is committed
        if error is None:
            email = await Vault(keys, ctx).decrypt(
                "member_invitation",
                "email_enc",
                invitation.invitation_id,
                invitation.email_enc,
                tenant_id=invitation.tenant_id,
            )
            if invitation.status == "claimed":
                ctx.audit(
                    "invitation_claim",
                    "denied",
                    "member_invitation",
                    str(invitation.invitation_id),
                    reason="already_claimed",
                )
                result = Claimed(state="already_claimed", email=email)
            else:
                account = await directory.create_or_find(email)
                await ctx.conn.execute(CLAIM_SQL, invitation.invitation_id, directory.issuer, account.sub)
                ctx.audit(
                    "invitation_claim",
                    "allowed",
                    "member_invitation",
                    str(invitation.invitation_id),
                    reason=claim_reason(account),
                )
                result = Claimed(
                    state="created" if account.temporary_password else "existing",
                    email=email,
                    temporary_password=account.temporary_password,
                )
    if error is not None:
        raise error
    assert result is not None  # noqa: S101 — set on every path that reaches here
    return result


async def _recheck_first_owner(ctx: SystemContext, invitation: Any) -> None:
    """409 ``owner_exists`` / ``has_travellers`` when an AA-sent owner invitation no longer meets the first-owner rule
    (a ``denied`` event goes on the agency audit; the invitation stays claimed and nothing else is written)."""
    row = await ctx.conn.fetchrow(PLATFORM_OWNER_RECHECK_SQL, invitation.invitation_id)
    if row is None or not row["by_platform"]:
        return
    code = "owner_exists" if row["has_owner"] else "has_travellers" if row["holds_travellers"] else None
    if code is None:
        return
    ctx.audit("invitation_accept", "denied", "member_invitation", str(invitation.invitation_id), reason=code)
    if code == "owner_exists":
        raise Conflict(code, "the agency has an owner now; ask the owner for an invitation")
    raise Conflict(code, "the agency holds client data now; a new owner comes only through an owner recovery")


async def accept(pool: asyncpg.Pool, caller: Caller, token: str) -> dict[str, Any]:
    digest = token_hash_hex(token)
    result: dict[str, Any] | None = None
    error: ServiceError | None = None
    async with invitation_tx(pool, digest) as (ctx, invitation):
        try:
            await _refuse_if_closed(ctx, invitation, accepting=True)
        except _CommitThen as wrapped:
            error = wrapped.error
        if error is None:
            if (caller.iss, caller.sub) != (invitation.cognito_iss, invitation.cognito_sub):
                ctx.audit(
                    "invitation_accept",
                    "denied",
                    "member_invitation",
                    str(invitation.invitation_id),
                    reason="identity_mismatch",
                )
                raise Forbidden(
                    "invitation_identity_mismatch", "sign in with the account this invitation was opened for"
                )
            existing = await ctx.conn.fetchrow(MEMBER_BY_IDENTITY_SQL, caller.iss, caller.sub)
            if existing is not None and existing["status"] == "active":
                raise Conflict("already_member", "you already belong to this agency; the owner changes roles")
            if invitation.role == "owner":
                await _recheck_first_owner(ctx, invitation)
            try:
                if existing is None:
                    member_id = await ctx.conn.fetchval(MEMBER_INSERT_SQL, caller.iss, caller.sub, invitation.role)
                else:
                    member_id = existing["member_id"]
                    await ctx.conn.execute(MEMBER_REACTIVATE_SQL, member_id, invitation.role)
                await ctx.conn.execute(ACCEPT_SQL, invitation.invitation_id, member_id)
            except asyncpg.PostgresError as exc:
                raise _accept_conflict(exc) from None
            ctx.audit(
                "invitation_accept",
                "allowed",
                "member",
                str(member_id),
                fields=("role", "status"),
                reason=f"role_{invitation.role}",
            )
            result = {
                "tenant_id": str(invitation.tenant_id),
                "member_id": str(member_id),
                "member_role": invitation.role,
            }
    if error is not None:
        raise error
    assert result is not None  # noqa: S101 — set on every path that reaches here
    return result
