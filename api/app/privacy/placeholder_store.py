"""The F3 placeholder map (crm-009 ``crm.placeholder_map``; design v3 §4.5, lane §2.9 / §5.4).

- :func:`seal_map` encrypts ``placeholder -> original`` as JSON under the tenant **scrub** DEK (AAD = tenant,
  ``placeholder_map``, ``map_enc``, the row id) **before** the model is called, so a key failure refuses the call
  instead of losing the map after paying for it;
- :func:`store_map` writes the row after the call is logged (same transaction, ``llm_call_id`` = that call), expiring
  24 h later (CHECK in crm-009);
- :func:`restore_placeholders` puts the values back into a model output **only in an agency member's session**
  (:class:`~app.tenancy.TenantContext`; a job's ``SystemContext`` is refused): an expired, purged or trashed map is
  410 ``placeholder_map_expired``, and every restore writes one ``placeholder_restore`` audit event (ids only).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from app.errors import Forbidden, Gone
from app.privacy.scrubber import restore
from app.tenancy import AnyContext, TenantContext

TABLE = "placeholder_map"
COLUMN = "map_enc"
PURPOSE = "scrub"
MAX_PLACEHOLDERS = 500

INSERT_SQL = """
INSERT INTO crm.placeholder_map (placeholder_map_id, llm_call_id, map_enc, placeholder_count, scrubber_version)
VALUES ($1, $2, $3, $4, $5)
"""
LOAD_SQL = """
SELECT map_enc FROM crm.placeholder_map
 WHERE placeholder_map_id = $1 AND expires_at > now() AND deleted_at IS NULL
"""


@dataclass(frozen=True)
class SealedMap:
    placeholder_map_id: uuid.UUID
    map_enc: bytes = field(repr=False)
    count: int


async def seal_map(vault, placeholders: dict[str, str]) -> SealedMap:  # noqa: ANN001 — app.vault.Vault
    if not 1 <= len(placeholders) <= MAX_PLACEHOLDERS:
        raise ValueError("a placeholder map holds 1..500 entries")
    map_id = uuid.uuid4()
    blob = await vault.encrypt(TABLE, COLUMN, map_id, json.dumps(placeholders, ensure_ascii=False), purpose=PURPOSE)
    return SealedMap(map_id, blob, len(placeholders))


async def store_map(ctx: AnyContext, sealed: SealedMap, llm_call_id: str, scrubber_version: str) -> None:
    await ctx.conn.execute(
        INSERT_SQL,
        sealed.placeholder_map_id,
        uuid.UUID(str(llm_call_id)),
        sealed.map_enc,
        sealed.count,
        scrubber_version,
    )


async def restore_placeholders(ctx: TenantContext, vault, placeholder_map_id: uuid.UUID, text: str) -> str:  # noqa: ANN001
    """``text`` with its placeholders replaced by the stored values (agency session only, audited)."""
    if not isinstance(ctx, TenantContext) or ctx.member_id is None:
        raise Forbidden("agency_session_required", "placeholders are restored only in an agency member's session")
    blob = await ctx.conn.fetchval(LOAD_SQL, placeholder_map_id)
    if blob is None:
        ctx.audit("placeholder_restore", "denied", TABLE, str(placeholder_map_id), reason="expired")
        raise Gone("placeholder_map_expired", "the placeholder map expired (24 h); write the reply by hand")
    mapping = json.loads(
        await vault.decrypt(TABLE, COLUMN, placeholder_map_id, bytes(blob), purpose=PURPOSE, tenant_id=ctx.tenant_id)
    )
    ctx.audit("placeholder_restore", "allowed", TABLE, str(placeholder_map_id))
    return restore(text, mapping)
