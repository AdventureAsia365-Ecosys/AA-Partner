"""Intake forms of an agency (``crm.form_definition``, ``crm.intake_form``; crm-005): create, list, change.

Used by the agency routes (Settings > Forms & embed, inside ``tenant_tx()``) and the Dev seeder (inside
``provisioning_tx()``). Every function takes the open context; none opens one (DB access lint).

- The form definition is the AA-prebuilt Stage 1 form (:mod:`app.intake.stage1`): one row per agency and version,
  created on first use.
- A form's ``public_key`` is random (``secrets.token_urlsafe(16)``, 22 characters): it is in the hosted URL and the
  embed code, so it is not a secret, but it cannot be guessed from another form's key.
- ``honeypot_field`` is a plausible field name picked at random from :data:`HONEYPOT_NAMES` per form, so a bot that
  learned one form's trap does not know the next one.
- ``allowed_origins``: https origins that may frame the hosted form (the agency's own website). Normalised to
  ``https://host[:port]``, at most 10.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.errors import Invalid, NotFound
from app.intake import stage1

if TYPE_CHECKING:  # pragma: no cover
    from app.tenancy import AnyContext, ProvisioningContext

HONEYPOT_NAMES = ("website", "company_url", "fax_number", "homepage", "office_phone", "address_extra")
MAX_ORIGINS = 10
MAX_FORMS = 20
_ORIGIN = re.compile(r"^https://([a-z0-9-]+\.)+[a-z]{2,63}(:[0-9]{1,5})?$")
_LEAD_SOURCE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

DEFINITION_SQL = (
    "SELECT form_definition_id FROM crm.form_definition WHERE stage = 's1' AND version = $1 AND deleted_at IS NULL"
)
INSERT_DEFINITION_SQL = """
INSERT INTO crm.form_definition (stage, version, schema_key, schema) VALUES ('s1', $1, $2, $3::jsonb)
ON CONFLICT (tenant_id, stage, version) DO NOTHING
RETURNING form_definition_id
"""
FORM_COLUMNS = (
    "intake_form_id, public_key, label, allowed_origins, honeypot_field, default_owner_member_id, lead_source, "
    "privacy_notice_url, status, created_at"
)
LIST_SQL = f"SELECT {FORM_COLUMNS} FROM crm.intake_form WHERE deleted_at IS NULL ORDER BY created_at"  # noqa: S608
GET_SQL = f"SELECT {FORM_COLUMNS} FROM crm.intake_form WHERE intake_form_id = $1 AND deleted_at IS NULL"  # noqa: S608
COUNT_SQL = "SELECT count(*) FROM crm.intake_form WHERE deleted_at IS NULL"
INSERT_FORM_SQL = f"""
INSERT INTO crm.intake_form (public_key, label, form_definition_id, allowed_origins, honeypot_field,
                             default_owner_member_id, lead_source, privacy_notice_url)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING {FORM_COLUMNS}
"""  # noqa: S608 — FORM_COLUMNS is a constant
ACTIVE_MEMBER_SQL = "SELECT 1 FROM crm.member WHERE member_id = $1 AND status = 'active' AND deleted_at IS NULL"


@dataclass(frozen=True)
class FormView:
    intake_form_id: str
    public_key: str
    label: str
    allowed_origins: list[str]
    default_owner_member_id: str | None
    lead_source: str
    privacy_notice_url: str | None
    status: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> FormView:
        owner = row["default_owner_member_id"]
        return cls(
            intake_form_id=str(row["intake_form_id"]),
            public_key=row["public_key"],
            label=row["label"],
            allowed_origins=list(row["allowed_origins"] or ()),
            default_owner_member_id=str(owner) if owner else None,
            lead_source=row["lead_source"],
            privacy_notice_url=row["privacy_notice_url"],
            status=row["status"],
            created_at=row["created_at"].isoformat(),
        )


def normalise_origins(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ORIGINS:
        raise Invalid("invalid_origins", f"at most {MAX_ORIGINS} https origins")
    out: list[str] = []
    for item in raw:
        origin = str(item).strip().lower().rstrip("/") if isinstance(item, str) else ""
        if not _ORIGIN.match(origin):
            raise Invalid("invalid_origins", "each origin is https://host[:port], no path")
        if origin not in out:
            out.append(origin)
    return out


def normalise_privacy_url(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or not raw.startswith("https://") or len(raw) > 500 or any(c.isspace() for c in raw):
        raise Invalid("invalid_privacy_notice_url", "an https:// link")
    return raw


def normalise_label(raw: Any) -> str:
    label = raw.strip() if isinstance(raw, str) else ""
    if not 1 <= len(label) <= 80:
        raise Invalid("invalid_label", "1 to 80 characters")
    return label


async def ensure_stage1_definition(ctx: AnyContext | ProvisioningContext) -> uuid.UUID:
    """The agency's Stage 1 definition row (created on first use)."""
    found = await ctx.conn.fetchval(DEFINITION_SQL, stage1.VERSION)
    if found:
        return found
    created = await ctx.conn.fetchval(
        INSERT_DEFINITION_SQL, stage1.VERSION, stage1.SCHEMA_KEY, json.dumps(stage1.definition())
    )
    return created or await ctx.conn.fetchval(DEFINITION_SQL, stage1.VERSION)


async def list_forms(ctx: AnyContext | ProvisioningContext) -> list[FormView]:
    return [FormView.from_row(r) for r in await ctx.conn.fetch(LIST_SQL)]


async def create_form(
    ctx: AnyContext | ProvisioningContext,
    *,
    label: str,
    allowed_origins: list[str] | None = None,
    default_owner_member_id: uuid.UUID | None = None,
    lead_source: str = "website_form",
    privacy_notice_url: str | None = None,
) -> FormView:
    if int(await ctx.conn.fetchval(COUNT_SQL) or 0) >= MAX_FORMS:
        raise Invalid("too_many_forms", f"at most {MAX_FORMS} forms per agency")
    if not _LEAD_SOURCE.match(lead_source):
        raise Invalid("invalid_lead_source", "a code")
    if default_owner_member_id is not None and not await ctx.conn.fetchval(ACTIVE_MEMBER_SQL, default_owner_member_id):
        raise Invalid("invalid_owner", "an active member of this agency")
    definition_id = await ensure_stage1_definition(ctx)
    row = await ctx.conn.fetchrow(
        INSERT_FORM_SQL,
        secrets.token_urlsafe(16),
        normalise_label(label),
        definition_id,
        normalise_origins(allowed_origins or []),
        secrets.choice(HONEYPOT_NAMES),
        default_owner_member_id,
        lead_source,
        normalise_privacy_url(privacy_notice_url),
    )
    return FormView.from_row(row)


async def update_form(ctx: AnyContext, form_id: uuid.UUID, changes: dict[str, Any]) -> FormView:
    """Change label / status / allowed origins / default owner / privacy link (each optional)."""
    row = await ctx.conn.fetchrow(GET_SQL + " FOR UPDATE", form_id)
    if row is None:
        raise NotFound("form_not_found")
    sets: list[str] = []
    args: list[Any] = []

    def put(column: str, value: Any) -> None:
        args.append(value)
        sets.append(f"{column} = ${len(args)}")

    if "label" in changes:
        put("label", normalise_label(changes["label"]))
    if "status" in changes:
        if changes["status"] not in ("active", "disabled"):
            raise Invalid("invalid_status", "active or disabled")
        if changes["status"] != row["status"]:
            put("status", changes["status"])
    if "allowed_origins" in changes:
        put("allowed_origins", normalise_origins(changes["allowed_origins"]))
    if "privacy_notice_url" in changes:
        put("privacy_notice_url", normalise_privacy_url(changes["privacy_notice_url"]))
    if "default_owner_member_id" in changes:
        owner = changes["default_owner_member_id"]
        owner_id = uuid.UUID(str(owner)) if owner else None
        if owner_id is not None and not await ctx.conn.fetchval(ACTIVE_MEMBER_SQL, owner_id):
            raise Invalid("invalid_owner", "an active member of this agency")
        put("default_owner_member_id", owner_id)
    if not sets:
        return FormView.from_row(row)
    args.append(form_id)
    updated = await ctx.conn.fetchrow(
        # Column names are fixed literals above; values are bind parameters.
        f"UPDATE crm.intake_form SET {', '.join(sets)} WHERE intake_form_id = ${len(args)} "  # noqa: S608
        f"RETURNING {FORM_COLUMNS}",
        *args,
    )
    return FormView.from_row(updated)
