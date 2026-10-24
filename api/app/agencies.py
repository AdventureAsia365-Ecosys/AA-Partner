"""Agency provisioning: the rules for creating a CRM agency (``crm.tenant``) and its first members (AA-935, CRM-07).

Used by the Dev seeder (``app.seed``) now and by the L1 console's "create agency" (``/crm/ops/v1``, CRM-13a / AA-944)
later, so both refuse the same things the same way:

- **Dev holds synthetic agencies only** (ADR 0010 decision 3; card CRM-07 R test): on ``dev`` / ``test`` / ``local`` an
  agency with ``is_synthetic = false`` is refused with **422** ``synthetic_required``. On ``prod`` a synthetic agency is
  refused with 422 ``synthetic_not_allowed`` (design v3 CRM-38: "prod refuses synthetic tenants, Dev refuses real
  ones").
- the label is trimmed and NFC-normalised (a Vietnamese name typed with combining marks and one typed precomposed are
  the same name, so the seeder stays idempotent and the name round-trips byte for byte), 1–120 characters, no control
  characters; the default locale is one of :data:`LOCALES`;
- a member is the token identity ``(iss, sub)`` and a CRM role, never an e-mail (ADR 0006b decision 5).

Writes run in :func:`app.tenancy.provisioning_tx` (``app.tenant_id`` = the new agency, so the crm-002 FORCE RLS policy
admits exactly that agency's rows). Creating the same agency twice converges instead of failing: the label, locale and
roles are brought to the spec, ``is_synthetic`` never flips (409), a disabled member is never re-activated here.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass

import asyncpg

from app.errors import Conflict, Invalid
from app.tenancy import MEMBER_ROLES, provisioning_tx

log = logging.getLogger(__name__)

LOCALES: tuple[str, ...] = ("en-AU", "en-GB", "en-US", "vi-VN")
SYNTHETIC_ONLY_ENVIRONMENTS = frozenset({"dev", "test", "local"})
REAL_ONLY_ENVIRONMENTS = frozenset({"prod"})
MAX_NAME_LENGTH = 120
_ISS_RE = re.compile(r"^https://[^/\s]+/[^/\s]+$")  # crm.member member_iss_chk
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class MemberSpec:
    cognito_iss: str
    cognito_sub: str
    role: str


@dataclass(frozen=True)
class AgencySpec:
    tenant_id: uuid.UUID
    display_name: str
    default_locale: str = "en-AU"
    is_synthetic: bool = True


@dataclass(frozen=True)
class ProvisionResult:
    tenant_id: uuid.UUID
    created: bool
    updated: bool
    members_added: int
    members_updated: int


def normalise_name(raw: str) -> str:
    """NFC, trimmed, inner whitespace runs collapsed; refuses empty, too long or control characters (422)."""
    name = " ".join(unicodedata.normalize("NFC", raw or "").split())
    if not name or len(name) > MAX_NAME_LENGTH or _CONTROL.search(raw or ""):
        raise Invalid("invalid_display_name", f"agency name must be 1-{MAX_NAME_LENGTH} printable characters")
    return name


def check_environment(environment: str, is_synthetic: bool) -> None:
    if environment in SYNTHETIC_ONLY_ENVIRONMENTS and not is_synthetic:
        raise Invalid(
            "synthetic_required", "this environment holds synthetic agencies only (is_synthetic must be true)"
        )
    if environment in REAL_ONLY_ENVIRONMENTS and is_synthetic:
        raise Invalid("synthetic_not_allowed", "production holds real agencies only")


def validate_agency(spec: AgencySpec, environment: str) -> AgencySpec:
    """The spec as it will be stored, or :class:`~app.errors.Invalid` (HTTP 422)."""
    check_environment(environment, spec.is_synthetic)
    if not isinstance(spec.tenant_id, uuid.UUID):
        raise Invalid("invalid_tenant_id", "tenant_id must be a UUID")
    if spec.default_locale not in LOCALES:
        raise Invalid("invalid_locale", f"default_locale must be one of {', '.join(LOCALES)}")
    return AgencySpec(
        tenant_id=spec.tenant_id,
        display_name=normalise_name(spec.display_name),
        default_locale=spec.default_locale,
        is_synthetic=spec.is_synthetic,
    )


def validate_member(member: MemberSpec) -> MemberSpec:
    if member.role not in MEMBER_ROLES:
        raise Invalid("invalid_role", f"role must be one of {', '.join(sorted(MEMBER_ROLES))}")
    if not _ISS_RE.match(member.cognito_iss or ""):
        raise Invalid("invalid_issuer", "cognito_iss must be a token issuer URL")
    sub = (member.cognito_sub or "").strip()
    if not 1 <= len(sub) <= 128 or _CONTROL.search(sub):
        raise Invalid("invalid_subject", "cognito_sub must be 1-128 printable characters")
    return MemberSpec(cognito_iss=member.cognito_iss, cognito_sub=sub, role=member.role)


TENANT_INSERT_SQL = """
INSERT INTO crm.tenant (tenant_id, display_name, default_locale, is_synthetic)
VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id) DO NOTHING
RETURNING tenant_id
"""
TENANT_CURRENT_SQL = "SELECT display_name, default_locale, is_synthetic FROM crm.tenant WHERE tenant_id = $1"
TENANT_UPDATE_SQL = "UPDATE crm.tenant SET display_name = $2, default_locale = $3 WHERE tenant_id = $1"
MEMBER_INSERT_SQL = """
INSERT INTO crm.member (cognito_iss, cognito_sub, role, status)
VALUES ($1, $2, $3, 'active')
ON CONFLICT (tenant_id, cognito_iss, cognito_sub) DO NOTHING
RETURNING member_id
"""
MEMBER_ROLE_UPDATE_SQL = """
UPDATE crm.member SET role = $3
 WHERE cognito_iss = $1 AND cognito_sub = $2 AND role <> $3 AND deleted_at IS NULL
"""


async def provision_agency(
    pool: asyncpg.Pool,
    spec: AgencySpec,
    members: tuple[MemberSpec, ...] = (),
    *,
    environment: str,
    actor: str,
) -> ProvisionResult:
    """Create (or converge) one agency and its members in one transaction. Validation happens before any SQL."""
    agency = validate_agency(spec, environment)
    people = tuple(validate_member(m) for m in members)
    created = updated = False
    added = role_changes = 0
    async with provisioning_tx(pool, agency.tenant_id, actor=actor) as ctx:
        inserted = await ctx.conn.fetchval(
            TENANT_INSERT_SQL, agency.tenant_id, agency.display_name, agency.default_locale, agency.is_synthetic
        )
        if inserted is not None:
            created = True
        else:
            current = await ctx.conn.fetchrow(TENANT_CURRENT_SQL, agency.tenant_id)
            if current is None:  # trashed by someone else between the two statements, or invisible: never guess
                raise Conflict("tenant_unavailable", "the agency exists but cannot be read in this context")
            if current["is_synthetic"] != agency.is_synthetic:
                raise Conflict("synthetic_flag_mismatch", "an agency never changes between synthetic and real")
            if (current["display_name"], current["default_locale"]) != (agency.display_name, agency.default_locale):
                await ctx.conn.execute(TENANT_UPDATE_SQL, agency.tenant_id, agency.display_name, agency.default_locale)
                updated = True
        for person in people:
            member_id = await ctx.conn.fetchval(MEMBER_INSERT_SQL, person.cognito_iss, person.cognito_sub, person.role)
            if member_id is not None:
                added += 1
                continue
            status = await ctx.conn.execute(MEMBER_ROLE_UPDATE_SQL, person.cognito_iss, person.cognito_sub, person.role)
            if status.endswith(" 1"):
                role_changes += 1
    log.info(
        "agency_provisioned tenant_id=%s created=%s updated=%s members_added=%d members_updated=%d",
        agency.tenant_id,
        created,
        updated,
        added,
        role_changes,
    )
    return ProvisionResult(agency.tenant_id, created, updated, added, role_changes)
