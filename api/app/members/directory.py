"""The partner pool admin calls of the CRM API (root ADR 0006b decisions 1 and 5; AA-939).

The IAM allow-list of the API's task role on the partner pool is exactly ``AdminCreateUser``, ``ListUsers``,
``AdminDisableUser``, ``AdminUserGlobalSignOut`` and (S263, root ADR 0006b amendment A1) ``AdminDeleteUser``
(AA-CIS-Infra ``identity_partner.tf``). This module uses four:

- :meth:`PartnerDirectory.create_or_find` (claim step): ``AdminCreateUser`` with ``MessageAction=SUPPRESS`` (Cognito
  never mails anything), ``email_verified=true`` and a random temporary password that the claim page shows once; the
  person must set their own password and enrol an authenticator at the first sign-in (the pool requires MFA). An
  address that already has an account (a member of another agency) is found with ``ListUsers`` (exact e-mail
  filter; the pool's usernames are case-insensitive e-mails, so one address is one account) and gets no password.
  An existing account that never finished its first sign-in (``FORCE_CHANGE_PASSWORD``: the person never saw or
  never used the temporary password, or it lapsed after the pool's 7 days) gets a **new** temporary password through
  ``AdminCreateUser`` with ``MessageAction=RESEND`` (the same allow-listed action; S263 join fix). RESEND makes
  Cognito mail the new temporary password to the address too (its default invitation text): the address is the one
  the invitation link went to, so this opens nothing the link did not already open. Only the first claim of an
  invitation calls this: a claimed invitation is never claimed again (``already_claimed``), so a second tab or a
  double submit cannot replace the password the first claim is showing.
- :meth:`PartnerDirectory.global_sign_out` (member removed): ``AdminUserGlobalSignOut`` revokes the person's refresh
  tokens; their access token still lives up to its lifetime, but every request re-checks ``crm.member`` anyway.
- :meth:`PartnerDirectory.email_for` (owner recovery notice): ``ListUsers`` filtered by ``sub``.
- :meth:`PartnerDirectory.account_state` and :meth:`PartnerDirectory.delete_never_signed_in` (S263, the partner-account
  clean-up, :mod:`app.members.accounts`): ``ListUsers`` filtered by ``sub`` for the status and the last change, then
  ``AdminDeleteUser``, only for an account that never finished its first sign-in (``FORCE_CHANGE_PASSWORD``) and was
  not changed after ``untouched_since``. The state is read again right before the delete, here, so whatever the caller
  decided, an account with its own password (``CONFIRMED`` or any other status) is never deleted by this code.

``AdminDisableUser`` is not called by the app: a person may belong to several agencies, and removal from one is a
membership change, not an account change.

Configuration: ``COGNITO_PARTNER_POOL_ID`` (from Terraform; never a code constant, public repo). Without it, outside
test / local, the invitation routes answer 503 ``directory_not_configured``; test / local use
:class:`MemoryDirectory`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import string
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from app.errors import Conflict, Unavailable

# The partner pool's password policy (identity_partner.tf): >= 14 characters, all four classes. The temporary
# password is 24 characters with each class present.
TEMP_PASSWORD_LENGTH = 24
_SYMBOLS = "!#$%&*+-=?@^_"
# The Cognito status of an account created by AdminCreateUser until its first sign-in sets the person's own password.
FORCE_CHANGE_PASSWORD = "FORCE_CHANGE_PASSWORD"  # noqa: S105 — a Cognito UserStatus, not a password


def temporary_password() -> str:
    classes = (string.ascii_lowercase, string.ascii_uppercase, string.digits, _SYMBOLS)
    alphabet = "".join(classes)
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(TEMP_PASSWORD_LENGTH))
        if all(any(c in cls for c in value) for cls in classes):
            return value


@dataclass(frozen=True)
class AccountState:
    """What the clean-up needs to know about a partner-pool account: its Cognito ``UserStatus`` and when it last
    changed (``UserLastModifiedDate``; a claim's ``RESEND`` changes it)."""

    status: str
    last_modified: dt.datetime | None


# The outcomes of :meth:`PartnerDirectory.delete_never_signed_in`.
DELETED = "deleted"
GONE = "gone"
SIGNED_IN = "signed_in"
CHANGED_SINCE = "changed_since"


def delete_verdict(state: AccountState | None, untouched_since: dt.datetime) -> str | None:
    """None when the account may be deleted, else the outcome code that keeps it."""
    if state is None:
        return GONE
    if state.status != FORCE_CHANGE_PASSWORD:
        return SIGNED_IN
    if state.last_modified is None or state.last_modified > untouched_since:
        return CHANGED_SINCE
    return None


@dataclass(frozen=True)
class Account:
    """The partner-pool account of an invited address. ``temporary_password`` when the claim created the account or
    gave an account that never finished its first sign-in a new one (``reissued``)."""

    sub: str
    temporary_password: str | None
    reissued: bool = False


class PartnerDirectory(Protocol):
    issuer: str

    async def create_or_find(self, email: str) -> Account: ...

    async def global_sign_out(self, sub: str) -> None: ...

    async def email_for(self, sub: str) -> str | None: ...

    async def account_state(self, sub: str) -> AccountState | None: ...

    async def delete_never_signed_in(self, sub: str, *, untouched_since: dt.datetime) -> str: ...


def _attr(user: dict[str, Any], name: str) -> str | None:
    for a in user.get("Attributes") or user.get("UserAttributes") or []:
        if a.get("Name") == name:
            return a.get("Value")
    return None


def _quoted(value: str) -> str:
    """A value for a ListUsers filter (double quotes and backslashes are not valid in an e-mail or a sub here)."""
    if '"' in value or "\\" in value:
        raise ValueError("unsafe filter value")
    return f'"{value}"'


class CognitoDirectory:
    """The partner pool through boto3 (blocking calls run in a worker thread)."""

    def __init__(self, pool_id: str, issuer: str, client: Any = None) -> None:
        self.pool_id = pool_id
        self.issuer = issuer
        self._region = pool_id.split("_", 1)[0]
        self._client = client
        self._lock = threading.Lock()

    def _idp(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3

                self._client = boto3.client("cognito-idp", region_name=self._region)
            return self._client

    def _find(self, email: str) -> dict[str, Any] | None:
        users = self._idp().list_users(UserPoolId=self.pool_id, Filter=f"email = {_quoted(email)}", Limit=2)["Users"]
        if len(users) > 1:  # cannot happen with e-mail usernames; refuse rather than pick one
            raise Conflict("account_ambiguous", "more than one account has this address")
        return users[0] if users else None

    def _create_or_find(self, email: str) -> Account:
        idp = self._idp()
        password = temporary_password()
        try:
            created = idp.admin_create_user(
                UserPoolId=self.pool_id,
                Username=email,
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
                TemporaryPassword=password,
                MessageAction="SUPPRESS",
            )["User"]
        except idp.exceptions.UsernameExistsException:
            found = self._find(email)
            if found is None:
                raise Unavailable("directory_unavailable", "the account could not be read") from None
            sub = _attr(found, "sub")
            if not sub:
                raise Unavailable("directory_unavailable", "the account has no subject") from None
            if found.get("UserStatus") == FORCE_CHANGE_PASSWORD:
                return Account(sub=sub, temporary_password=self._resend(email), reissued=True)
            return Account(sub=sub, temporary_password=None)
        sub = _attr(created, "sub")
        if not sub:
            raise Unavailable("directory_unavailable", "the new account has no subject")
        return Account(sub=sub, temporary_password=password)

    def _resend(self, email: str) -> str:
        """A new temporary password for an account that never finished its first sign-in: ``AdminCreateUser`` with
        ``MessageAction=RESEND`` (Cognito also mails it to the address; ``EMAIL``, never the SMS default)."""
        password = temporary_password()
        # The partner pool uses e-mail usernames: AdminCreateUser takes the address, never the internal username (the
        # sub), or Cognito answers "Username should be an email" (S263 live).
        self._idp().admin_create_user(
            UserPoolId=self.pool_id,
            Username=email,
            TemporaryPassword=password,
            MessageAction="RESEND",
            DesiredDeliveryMediums=["EMAIL"],
        )
        return password

    async def create_or_find(self, email: str) -> Account:
        return await asyncio.to_thread(self._create_or_find, email)

    async def global_sign_out(self, sub: str) -> None:
        # E-mail-username pools: the internal username is the sub.
        await asyncio.to_thread(self._idp().admin_user_global_sign_out, UserPoolId=self.pool_id, Username=sub)

    async def email_for(self, sub: str) -> str | None:
        def read() -> str | None:
            users = self._idp().list_users(UserPoolId=self.pool_id, Filter=f"sub = {_quoted(sub)}", Limit=1)["Users"]
            return _attr(users[0], "email") if users else None

        return await asyncio.to_thread(read)

    def _by_sub(self, sub: str) -> dict[str, Any] | None:
        users = self._idp().list_users(UserPoolId=self.pool_id, Filter=f"sub = {_quoted(sub)}", Limit=2)["Users"]
        if len(users) > 1:  # a sub is unique in a pool; refuse rather than pick one
            raise Conflict("account_ambiguous", "more than one account has this subject")
        return users[0] if users else None

    @staticmethod
    def _state(user: dict[str, Any] | None) -> AccountState | None:
        if user is None:
            return None
        modified = user.get("UserLastModifiedDate")
        return AccountState(
            status=str(user.get("UserStatus") or ""),
            last_modified=modified if isinstance(modified, dt.datetime) else None,
        )

    async def account_state(self, sub: str) -> AccountState | None:
        return await asyncio.to_thread(lambda: self._state(self._by_sub(sub)))

    def _delete_never_signed_in(self, sub: str, untouched_since: dt.datetime) -> str:
        user = self._by_sub(sub)
        refused = delete_verdict(self._state(user), untouched_since)
        if refused is not None:
            return refused
        idp = self._idp()
        try:
            idp.admin_delete_user(UserPoolId=self.pool_id, Username=user["Username"])  # type: ignore[index]
        except idp.exceptions.UserNotFoundException:
            return GONE
        return DELETED

    async def delete_never_signed_in(self, sub: str, *, untouched_since: dt.datetime) -> str:
        """``AdminDeleteUser`` of the account with this ``sub`` only when it is still ``FORCE_CHANGE_PASSWORD`` and
        unchanged since ``untouched_since`` (read again here, right before the delete). Returns :data:`DELETED`,
        :data:`GONE` (no such account), :data:`SIGNED_IN` or :data:`CHANGED_SINCE`."""
        return await asyncio.to_thread(self._delete_never_signed_in, sub, untouched_since)


class MemoryDirectory:
    """In-process stand-in for tests and local runs (never configured on dev / stg / prod)."""

    def __init__(self, issuer: str = "https://cognito-idp.local.test/local_partner") -> None:
        self.issuer = issuer
        self.accounts: dict[str, dict[str, Any]] = {}
        self.signed_out: list[str] = []
        self.resent: list[str] = []
        self.deleted: list[str] = []

    def add(self, email: str, *, status: str = "CONFIRMED", modified: dt.datetime | None = None) -> str:
        sub = str(uuid.uuid4())
        self.accounts[email.lower()] = {"sub": sub, "status": status, "modified": modified or _now()}
        return sub

    async def create_or_find(self, email: str) -> Account:
        found = self.accounts.get(email.lower())
        if found is not None:
            if found["status"] == FORCE_CHANGE_PASSWORD:
                return Account(sub=found["sub"], temporary_password=self._resend(found), reissued=True)
            return Account(sub=found["sub"], temporary_password=None)
        sub = self.add(email, status=FORCE_CHANGE_PASSWORD)
        account = self.accounts[email.lower()]
        account["password"] = temporary_password()
        return Account(sub=sub, temporary_password=account["password"])

    def _resend(self, account: dict[str, Any]) -> str:
        account["password"] = temporary_password()
        account["modified"] = _now()
        self.resent.append(account["sub"])
        return account["password"]

    async def global_sign_out(self, sub: str) -> None:
        self.signed_out.append(sub)

    async def email_for(self, sub: str) -> str | None:
        return next((e for e, a in self.accounts.items() if a["sub"] == sub), None)

    async def account_state(self, sub: str) -> AccountState | None:
        found = next((a for a in self.accounts.values() if a["sub"] == sub), None)
        return None if found is None else AccountState(status=found["status"], last_modified=found.get("modified"))

    async def delete_never_signed_in(self, sub: str, *, untouched_since: dt.datetime) -> str:
        refused = delete_verdict(await self.account_state(sub), untouched_since)
        if refused is not None:
            return refused
        email = next(e for e, a in self.accounts.items() if a["sub"] == sub)
        del self.accounts[email]
        self.deleted.append(sub)
        return DELETED


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def require_directory(directory: PartnerDirectory | None) -> PartnerDirectory:
    if directory is None:
        raise Unavailable("directory_not_configured", "the partner pool is not configured")
    return directory
