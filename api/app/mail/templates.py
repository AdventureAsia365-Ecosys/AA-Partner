"""The agency's message template library (card CRM-22, AA-951; design v3 R9, group G ``message_template``; crm-022).

- **Read** (every role): the templates (title, locale, source, the body decrypted) and the AA starter templates the
  agency has not copied yet (``aa_starters``: from the CIS content cache, ``crm.aa_content``).
- **Write** (owner, ``WORKFLOW_EDIT``: the agency's templates): create, edit, delete for good (agency text, no client
  data by design; the body is still sealed under the mail DEK, as a saved reply, since agents may type names into it).
- **Import AA starters** (owner): each CIS starter template (every locale CIS has) becomes the agency's own copy
  (source ``aa_starter``, the group, content id and version it came from); one copy per group and locale, so a second
  import adds only what is new and never overwrites the agency's edits.

Templates start a message: the text proposal (A-05) merges one around the proposal (:mod:`app.proposals.text_render`).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.catalog import store as catalog
from app.errors import Invalid, NotFound
from app.permissions import Action, require
from app.tenancy import TenantContext
from app.vault import Vault

PURPOSE = "mail"
MAX_TITLE = 120
MAX_BODY = 20_000

LIST_SQL = """
SELECT message_template_id, locale, title, body_enc, source, aa_group_key, aa_version, updated_at
  FROM crm.message_template WHERE deleted_at IS NULL
 ORDER BY locale, title, message_template_id
 LIMIT 300
"""
ONE_SQL = """
SELECT message_template_id, locale, title, body_enc, source, aa_group_key FROM crm.message_template
 WHERE message_template_id = $1 AND deleted_at IS NULL
"""
INSERT_SQL = """
INSERT INTO crm.message_template (message_template_id, locale, title, body_enc, source, aa_group_key, aa_content_id,
                                  aa_version)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (tenant_id, aa_group_key, locale) WHERE aa_group_key IS NOT NULL DO NOTHING
RETURNING message_template_id
"""
UPDATE_SQL = """
UPDATE crm.message_template SET locale = coalesce($2, locale), title = coalesce($3, title),
       body_enc = coalesce($4, body_enc)
 WHERE message_template_id = $1
"""
DELETE_SQL = "DELETE FROM crm.message_template WHERE message_template_id = $1"
IMPORTED_SQL = "SELECT aa_group_key, locale FROM crm.message_template WHERE aa_group_key IS NOT NULL"


def _title(value: str) -> str:
    clean = " ".join((value or "").split())
    if not clean:
        raise Invalid("title_required")
    if len(clean) > MAX_TITLE:
        raise Invalid("title_too_long", f"at most {MAX_TITLE} characters")
    return clean


def _body(value: str) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\x00", "").strip()
    if not text:
        raise Invalid("body_required")
    if len(text) > MAX_BODY:
        raise Invalid("body_too_long", f"at most {MAX_BODY} characters")
    return text


async def _open(vault: Vault, row: Any) -> str:
    return await vault.decrypt("message_template", "body_enc", row["message_template_id"], row["body_enc"],
                               purpose=PURPOSE)  # fmt: skip


async def library(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    rows = await ctx.conn.fetch(LIST_SQL)
    templates = [
        {
            "message_template_id": str(r["message_template_id"]),
            "locale": r["locale"],
            "title": r["title"],
            "body": await _open(vault, r),
            "source": r["source"],
            "aa_group_key": r["aa_group_key"],
            "aa_version": r["aa_version"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]
    imported = {(r["aa_group_key"], r["locale"]) for r in await ctx.conn.fetch(IMPORTED_SQL)}
    starters = [
        {"group_key": s["group_key"], "locale": s["locale"], "title": s["title"], "version": s["version"]}
        for s in await catalog.starter_templates(ctx)
        if (s["group_key"], s["locale"]) not in imported
    ]
    ctx.audit("view", "allowed", "message_template", None, fields=("templates",))
    return {"templates": templates, "aa_starters": starters}


async def get(ctx: TenantContext, vault: Vault, template_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, template_id)
    if row is None:
        raise NotFound("template_not_found")
    return {"message_template_id": str(template_id), "locale": row["locale"], "title": row["title"],
            "body": await _open(vault, row)}  # fmt: skip


async def create(ctx: TenantContext, vault: Vault, *, locale: str, title: str, body: str) -> dict[str, Any]:
    require(ctx, Action.WORKFLOW_EDIT, object_kind="message_template")
    template_id = uuid.uuid4()
    blob = await vault.encrypt("message_template", "body_enc", template_id, _body(body), purpose=PURPOSE)
    await ctx.conn.fetchval(INSERT_SQL, template_id, locale, _title(title), blob, "agency", None, None, None)
    ctx.audit("message_template_create", "allowed", "message_template", str(template_id))
    return {"message_template_id": str(template_id)}


async def update(
    ctx: TenantContext, vault: Vault, template_id: uuid.UUID, *, locale: str | None, title: str | None,
    body: str | None,
) -> dict[str, Any]:  # fmt: skip
    require(ctx, Action.WORKFLOW_EDIT, object_kind="message_template", object_id=str(template_id))
    if await ctx.conn.fetchrow(ONE_SQL, template_id) is None:
        raise NotFound("template_not_found")
    blob = (
        await vault.encrypt("message_template", "body_enc", template_id, _body(body), purpose=PURPOSE)
        if body is not None
        else None
    )
    await ctx.conn.execute(UPDATE_SQL, template_id, locale, _title(title) if title is not None else None, blob)
    ctx.audit("message_template_edit", "allowed", "message_template", str(template_id))
    return {"message_template_id": str(template_id)}


async def delete(ctx: TenantContext, template_id: uuid.UUID) -> dict[str, Any]:
    require(ctx, Action.WORKFLOW_EDIT, object_kind="message_template", object_id=str(template_id))
    if await ctx.conn.fetchrow(ONE_SQL, template_id) is None:
        raise NotFound("template_not_found")
    await ctx.conn.execute(DELETE_SQL, template_id)
    ctx.audit("message_template_delete", "allowed", "message_template", str(template_id))
    return {"message_template_id": str(template_id), "status": "deleted"}


async def import_starters(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    """Copy every CIS starter template the agency does not have yet (per group and locale)."""
    require(ctx, Action.WORKFLOW_EDIT, object_kind="message_template")
    added = 0
    for s in await catalog.starter_templates(ctx):
        template_id = uuid.uuid4()
        text = _body(s["body_markdown"])[:MAX_BODY]
        blob = await vault.encrypt("message_template", "body_enc", template_id, text, purpose=PURPOSE)
        done = await ctx.conn.fetchval(
            INSERT_SQL, template_id, s["locale"], _title(s["title"])[:MAX_TITLE], blob, "aa_starter",
            s["group_key"], uuid.UUID(str(s["content_id"])), s["version"],
        )  # fmt: skip
        added += done is not None
    ctx.audit("message_template_import", "allowed", "message_template", None, reason="aa_starter")
    return {"added": added}
