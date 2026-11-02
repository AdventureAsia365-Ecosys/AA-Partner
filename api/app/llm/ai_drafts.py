"""AI drafts of the trip workspace (AA-1027 slice F3, gap CRM-4; design v3 M14, R39 "AI drafts, humans send", A-21).

- :func:`ai_assist_allowed`: the agency's plan entitlement (``crm.tenant_entitlement.allowed_features`` holds
  ``ai_assist``, written by AA in the L1 console). Without it the draft endpoint answers 403 ``feature_not_allowed``
  before anything is read, scrubbed or paid, and the workspace shows the Draft action disabled with the reason.
- :func:`store`: one ``crm.ai_draft`` row (crm-040) for each draft the gateway answered (outcome ``ok``): the kind,
  the ledger call, the prompt version and the draft **as the model wrote it** (placeholders kept, never restored at
  rest) under the agency's vault DEK. No prompt text is stored: the ledger row keeps its hash.
- :func:`decide`: the member's one decision on a pending draft. ``accept`` needs ``ai_draft_approve`` (owner, agent;
  an assistant only on a trip assigned to them, like ``message_send``); ``discard`` is every role's (``ai_use``).
  Either may carry a feedback code from :data:`FEEDBACK_CODES` (R39). Accepting sends nothing: the member copies the
  draft into a message and sends it themselves. The database guard (crm-040 ``crm.guard_ai_draft``, CRM47) holds the
  same rules (once, from pending, a member only).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.permissions import Action, require
from app.tenancy import TenantContext

FEATURE = "ai_assist"
DECISIONS = {"accept": "accepted", "discard": "discarded"}
FEEDBACK_CODES = ("good", "wrong_facts", "wrong_tone", "not_relevant", "too_long", "other")

ENTITLEMENT_SQL = """
SELECT coalesce((SELECT $1 = ANY (allowed_features) FROM crm.tenant_entitlement WHERE deleted_at IS NULL), false)
"""
INSERT_SQL = """
INSERT INTO crm.ai_draft (ai_draft_id, trip_id, kind, llm_call_id, prompt_version, output_enc)
VALUES ($1, $2, $3, $4, $5, $6)
"""
DRAFT_SQL = """
SELECT d.ai_draft_id, d.trip_id, d.decision, t.owner_member_id
FROM crm.ai_draft d JOIN crm.trip t ON t.trip_id = d.trip_id
WHERE d.ai_draft_id = $1
"""
DECIDE_SQL = """
UPDATE crm.ai_draft
SET decision = $2, feedback_code = $3, decided_at = now(), decided_by = current_setting('app.actor', true)
WHERE ai_draft_id = $1 AND decision = 'pending'
RETURNING ai_draft_id, trip_id, decision, feedback_code, decided_at
"""


async def ai_assist_allowed(ctx: TenantContext) -> bool:
    return bool(await ctx.conn.fetchval(ENTITLEMENT_SQL, FEATURE))


async def require_ai_assist(ctx: TenantContext, trip_id: uuid.UUID) -> None:
    """403 ``feature_not_allowed`` (with a ``denied`` audit event) unless the agency's plan has the AI assistant."""
    if await ai_assist_allowed(ctx):
        return
    ctx.audit(Action.AI_USE.value, "denied", "trip", str(trip_id), reason="feature_not_allowed")
    raise Forbidden("feature_not_allowed", "the AI assistant is not on this agency's plan (ask AA to add it)")


async def store(
    ctx: TenantContext,
    vault: Any,
    trip_id: uuid.UUID,
    *,
    kind: str,
    llm_call_id: str,
    prompt_version: str,
    output: str,
) -> str:
    """Write the draft row (pending) and return its id. ``output`` is the model's text with its placeholders."""
    draft_id = uuid.uuid4()
    output_enc = await vault.encrypt("ai_draft", "output_enc", draft_id, output)
    await ctx.conn.execute(INSERT_SQL, draft_id, trip_id, kind, uuid.UUID(llm_call_id), prompt_version, output_enc)
    ctx.audit("create", "allowed", "ai_draft", str(draft_id), object_ids=(str(trip_id),))
    return str(draft_id)


async def decide(
    ctx: TenantContext, draft_id: uuid.UUID, *, action: str, feedback_code: str | None = None
) -> dict[str, Any]:
    if action not in DECISIONS:
        raise Invalid("invalid_decision", ", ".join(DECISIONS))
    if feedback_code is not None and feedback_code not in FEEDBACK_CODES:
        raise Invalid("invalid_feedback_code", ", ".join(FEEDBACK_CODES))
    row = await ctx.conn.fetchrow(DRAFT_SQL, draft_id)
    if row is None:
        raise NotFound("draft_not_found", "no AI draft with this id")
    if action == "accept":
        owner = row["owner_member_id"]
        require(
            ctx,
            Action.AI_DRAFT_APPROVE,
            object_kind="ai_draft",
            object_id=str(draft_id),
            assigned=owner is not None and owner == ctx.member_id,
        )
    if row["decision"] != "pending":
        raise Conflict("draft_decided", f"this draft was already {row['decision']}")
    out = await ctx.conn.fetchrow(DECIDE_SQL, draft_id, DECISIONS[action], feedback_code)
    if out is None:  # another request decided it in between
        raise Conflict("draft_decided", "this draft was already decided")
    ctx.audit(
        f"ai_draft_{action}", "allowed", "ai_draft", str(draft_id), reason=feedback_code,
        object_ids=(str(row["trip_id"]),),
    )  # fmt: skip
    return {
        "ai_draft_id": str(out["ai_draft_id"]),
        "trip_id": str(out["trip_id"]),
        "decision": out["decision"],
        "feedback_code": out["feedback_code"],
        "decided_at": out["decided_at"].isoformat(),
    }
