"""Ready to travel again (AA-960, card CRM-29b; design v3 R36, A-15 Marketing, A-07 client profile).

``GET /marketing/ready-to-travel`` (owner, agent: ``marketing_view``): the live, not archived clients the system tag
``sys_ready_to_travel`` is on (the tick sets it, :mod:`app.tags.system`), newest first, :data:`PAGE_SIZE` per page.
Each row explains itself (card E): the reasons of the tag (codes and numbers, worded by the web), the client's last
trip (title, dates), their owner, and the state of their marketing consent (``yes`` / ``no`` / ``unknown``: a message
the agency sends from here is marketing, so it needs a yes; the dispatcher's gate checks it again). Names are opened
for the page and the view is audited (``list_view``, reason ``ready_to_travel``). The list is the agency's own
marketing (the agency is the controller); Adventure Asia never reads it.
"""

from __future__ import annotations

import json
from typing import Any

from app.errors import Invalid
from app.permissions import Action, require
from app.tags import model
from app.tenancy import TenantContext
from app.vault import Vault

PAGE_SIZE = 50
MAX_PAGE = 200

LIST_SQL = """
SELECT tr.traveler_id, tr.display_name_enc, tr.owner_member_id, l.reasons, l.created_at AS since,
       (SELECT c.value FROM crm.consent c WHERE c.traveler_id = tr.traveler_id AND c.purpose = 'marketing'
         ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC LIMIT 1) AS marketing,
       last.trip_id AS last_trip_id, last.title AS last_trip_title, last.start_date AS last_trip_start,
       last.end_date AS last_trip_end
  FROM crm.tag_link l
  JOIN crm.tag g ON g.tag_id = l.tag_id AND g.kind = 'system' AND g.code = $1
  JOIN crm.traveler tr ON tr.traveler_id = l.traveler_id AND tr.deleted_at IS NULL AND tr.archived_at IS NULL
  LEFT JOIN LATERAL (
        SELECT t.trip_id, t.title, t.start_date, t.end_date
          FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id AND t.deleted_at IS NULL
         WHERE tt.traveler_id = tr.traveler_id AND tt.deleted_at IS NULL AND t.stage = 'completed'
         ORDER BY t.end_date DESC NULLS LAST, t.trip_id
         LIMIT 1) last ON true
 WHERE ($2::uuid IS NULL OR tr.owner_member_id = $2)
 ORDER BY l.created_at DESC, tr.traveler_id
 LIMIT $3 OFFSET $4
"""
COUNT_SQL = """
SELECT count(*)::int
  FROM crm.tag_link l
  JOIN crm.tag g ON g.tag_id = l.tag_id AND g.kind = 'system' AND g.code = $1
  JOIN crm.traveler tr ON tr.traveler_id = l.traveler_id AND tr.deleted_at IS NULL AND tr.archived_at IS NULL
 WHERE ($2::uuid IS NULL OR tr.owner_member_id = $2)
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _consent(value: Any) -> str:
    return "unknown" if value is None else "yes" if value else "no"


async def listing(ctx: TenantContext, vault: Vault, *, owner: str = "anyone", page: int = 1) -> dict[str, Any]:
    require(ctx, Action.MARKETING_VIEW, object_kind="traveler")
    if not 1 <= page <= MAX_PAGE:
        raise Invalid("invalid_page", f"1 to {MAX_PAGE}")
    if owner not in ("anyone", "me"):
        raise Invalid("invalid_owner", "anyone or me")
    owner_id = ctx.member_id if owner == "me" else None
    total = int(await ctx.conn.fetchval(COUNT_SQL, model.READY_TAG, owner_id) or 0)
    rows = await ctx.conn.fetch(LIST_SQL, model.READY_TAG, owner_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    items = []
    for r in rows:
        name = await vault.decrypt(
            "traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"], tenant_id=ctx.tenant_id
        )
        reasons = r["reasons"]
        items.append(
            {
                "traveler_id": str(r["traveler_id"]),
                "display_name": name,
                "owner_member_id": str(r["owner_member_id"]) if r["owner_member_id"] else None,
                "is_mine": r["owner_member_id"] == ctx.member_id,
                "reasons": (json.loads(reasons) if isinstance(reasons, str) else reasons) or [],
                "since": _iso(r["since"]),
                "marketing_consent": _consent(r["marketing"]),
                "last_trip": None
                if r["last_trip_id"] is None
                else {
                    "trip_id": str(r["last_trip_id"]),
                    "title": r["last_trip_title"],
                    "start_date": _iso(r["last_trip_start"]),
                    "end_date": _iso(r["last_trip_end"]),
                },
            }
        )
    if items:
        ctx.audit("list_view", "allowed", "traveler", None, fields=("display_name",), reason="ready_to_travel",
                  object_ids=tuple(i["traveler_id"] for i in items))  # fmt: skip
    return {
        "items": items,
        "total": total,
        "page": page,
        "has_more": page * PAGE_SIZE < total,
        "ready_after_months": model.READY_AFTER_MONTHS,
        "filter": {"owner": owner},
    }
