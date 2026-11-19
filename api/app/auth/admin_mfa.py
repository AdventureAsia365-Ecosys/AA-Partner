"""Administrator MFA check of the AA staff route classes (AA-1111 slice S2; root ADR 0006c; crm-082).

Tech lead decisions: S283 "admin MFA = TOTP" (an authenticator app code, never SMS), S285 "the WAF geo rule of the
staff hosts goes once admin MFA is enforced". The WORKFORCE pool allows TOTP as optional MFA; a staff member with TOTP
enrolled signs in with password + TOTP and Amazon Cognito writes the factors into the access token's ``amr`` claim
(``["pwd", "otp", "mfa"]``; an e-mail code sign-in gives ``["email_otp"]``). Cognito computes ``amr`` (a pre token
trigger cannot change it) and the verifier already checked the signature of this exact token string, so the claim is
read here from it (the verifier is a byte copy of AA-CIS-App's and its ``Principal`` carries no ``amr``).

Rule (:func:`check`, called by :mod:`app.auth.route_guard` after the class and group checks):

- only a WORKFORCE session holding an **operations group** is checked: :data:`ADMIN_GROUPS` (``crm-platform-admin``,
  ``crm-ops``, and ``cis-admin``, which reads every agency on ``/crm/ops/v1``). Agency members (PARTNER pool, MFA always
  required there, root ADR 0006b), the desk groups (``crm-support``, ``cis-customer_success``, ``cis-support``) and the
  Dev-only E2E identities (A10b / A10c; they cannot exist outside Dev) are never checked;
- MFA is satisfied when ``amr`` holds ``otp`` (:data:`MFA_AMR`);
- not satisfied and ``crm.platform.admin_mfa_required`` is **on** -> 403 ``mfa_required``;
- not satisfied and the switch is **off** (the default) -> served, logged ``staff_admin_mfa_check outcome=would_refuse``
  (``satisfied`` when the code was used), one line per identity and outcome per :data:`LOG_EVERY_S`.

The switch is read from the settings snapshot (never the database on the request path).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import jwt
from fastapi import HTTPException

from app import platform_settings as ps
from app.auth.identity_config import GROUP_CIS_ADMIN, GROUP_CRM_OPS, GROUP_CRM_PLATFORM_ADMIN, WORKFORCE

log = logging.getLogger(__name__)

MFA_REQUIRED = "mfa_required"
# The Cognito amr value of a TOTP code from an authenticator app (the tech lead chose TOTP, S283).
MFA_AMR = "otp"
ADMIN_GROUPS = frozenset({GROUP_CRM_PLATFORM_ADMIN, GROUP_CRM_OPS, GROUP_CIS_ADMIN})
LOG_EVERY_S = 600.0  # technical-constant: log throttle of the check line, not a policy value
LOG_TABLE_SIZE = 1024  # technical-constant: size of the throttle table

_logged: dict[tuple[str, str, str], float] = {}


def clear_log_throttle() -> None:
    _logged.clear()


def token_amr(token: Any) -> tuple[str, ...]:
    """The ``amr`` values of an already verified access token (empty when absent or malformed)."""
    if not isinstance(token, str) or not token:
        return ()
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return ()
    value = claims.get("amr") if isinstance(claims, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def mfa_used(amr: tuple[str, ...]) -> bool:
    return MFA_AMR in amr


def is_admin_session(issuer_kind: str | None, groups: tuple[str, ...], *, e2e: bool) -> bool:
    """A WORKFORCE session with an operations group, never a Dev-only E2E identity."""
    return issuer_kind == WORKFORCE and not e2e and bool(ADMIN_GROUPS.intersection(groups))


def required() -> bool:
    """The switch in force (settings snapshot)."""
    return bool(ps.get(ps.ADMIN_MFA_REQUIRED))


def _log_once(iss: str, sub: str, outcome: str, level: int, groups: tuple[str, ...], amr: tuple[str, ...]) -> None:
    now = time.monotonic()
    key = (iss, sub, outcome)
    last = _logged.get(key)
    if last is not None and now - last < LOG_EVERY_S:
        return
    if len(_logged) >= LOG_TABLE_SIZE:
        _logged.clear()
    _logged[key] = now
    admin_groups = sorted(ADMIN_GROUPS.intersection(groups))
    log.log(level, "staff_admin_mfa_check outcome=%s sub=%s groups=%s amr=%s", outcome, sub, admin_groups, list(amr))


def check(
    issuer_kind: str | None, iss: str, sub: str, groups: tuple[str, ...], token: str, *, e2e: bool = False
) -> None:
    """Refuse an operations staff session without an authenticator code while the switch is on (403 mfa_required)."""
    if not is_admin_session(issuer_kind, groups, e2e=e2e):
        return
    amr = token_amr(token)
    if mfa_used(amr):
        _log_once(iss, sub, "satisfied", logging.INFO, groups, amr)
        return
    if not required():
        _log_once(iss, sub, "would_refuse", logging.INFO, groups, amr)
        return
    _log_once(iss, sub, "refused", logging.WARNING, groups, amr)
    raise HTTPException(status_code=403, detail=MFA_REQUIRED)
