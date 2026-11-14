"""The shared steps of the CRM-33b AI runs that answer in JSON (AA-965: R10 supplier confirmations, R40 notes to
options).

- :func:`preflight`: the trip exists, the plan has ``ai_assist`` (403 ``feature_not_allowed``) and AI is on (409
  ``ai_disabled``), checked before the gateway so nothing is scrubbed or paid (the reply draft's order, CRM-33a).
- :func:`parse_object`: the first JSON object of a model answer (models may wrap it in a code fence or a sentence);
  None when there is none. The caller then checks every field against its closed list: nothing the model wrote is
  trusted as an id.
- :func:`finish`: the answered run's ``crm.ai_draft`` row (kind, ledger call, prompt version, the answer **as the model
  wrote it** under the vault DEK, the code's ``validation``) in a savepoint like the reply draft (a row that cannot be
  written never rolls back the paid ledger row), then the placeholders of the answer put back in the member's session
  (one ``placeholder_restore`` event). Returns (draft id or None, the restored answer or None).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from app.errors import Conflict, NotFound
from app.llm import ai_drafts
from app.llm.gateway import GatewayResult
from app.privacy.placeholder_store import restore_placeholders
from app.tenancy import TenantContext

log = logging.getLogger(__name__)

TRIP_SQL = "SELECT destinations FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
ENABLED_SQL = "SELECT ai_enabled FROM crm.tenant_settings WHERE deleted_at IS NULL"
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# Characters a model may not put in a short field we show (control characters, angle brackets outside a placeholder).
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


async def preflight(ctx: TenantContext, trip_id: uuid.UUID) -> list[str]:
    """404 / 403 / 409 before any AI work; returns the trip's destination codes."""
    destinations = await ctx.conn.fetchval(TRIP_SQL, trip_id)
    if destinations is None:
        raise NotFound("trip_not_found", "no trip with this id")
    await ai_drafts.require_ai_assist(ctx, trip_id)
    if not await ctx.conn.fetchval(ENABLED_SQL):
        raise Conflict("ai_disabled", "AI features are off for this agency (Settings > AI)")
    return [str(d) for d in destinations or ()]


def parse_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for chunk in candidates:
        start = chunk.find("{")
        while start != -1:
            try:
                value, _ = json.JSONDecoder().raw_decode(chunk[start:])
            except ValueError:
                start = chunk.find("{", start + 1)
                continue
            if isinstance(value, dict):
                return value
            start = chunk.find("{", start + 1)
    return None


def short_text(value: Any, limit: int) -> str | None:
    """A short model-written string we show (a reference, a time, a reason): stripped, no control characters, at most
    ``limit`` characters; None otherwise."""
    if not isinstance(value, str):
        return None
    text = CONTROL.sub(" ", value).strip()
    return text[:limit] if text else None


def tagged(tag: str, text: str) -> str:
    """Data between our tags; the data cannot close its own tag."""
    cleaned = text.replace(f"</{tag}>", f"</ {tag}>")
    return f"<{tag}>\n{cleaned.strip()}\n</{tag}>"


async def finish(
    ctx: TenantContext,
    vault: Any,
    result: GatewayResult,
    trip_id: uuid.UUID,
    *,
    kind: str,
    prompt_version: str,
    validation: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if result.outcome != "ok" or result.text is None or not result.llm_call_id:
        return None, None
    draft_id = None
    try:
        async with ctx.conn.transaction():
            draft_id = await ai_drafts.store(
                ctx, vault, trip_id, kind=kind, llm_call_id=result.llm_call_id, prompt_version=prompt_version,
                output=result.text, validation=validation,
            )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 — class only, never the text
        log.warning("crm ai %s: draft row not stored (%s), call stays logged", kind, type(exc).__name__)
        draft_id = None
    text = result.text
    if result.placeholder_map_id:
        text = await restore_placeholders(ctx, vault, uuid.UUID(result.placeholder_map_id), text)
    return draft_id, text


def outcome(result: GatewayResult) -> dict[str, Any]:
    """The gateway fields every CRM-33b run answers with (the reply draft's shape)."""
    return {
        "outcome": result.outcome,
        "fallback": result.fallback,
        "refusal_code": result.refusal_code,
        "error_class": result.error_class,
        "llm_call_id": result.llm_call_id,
        "budget_used_pct": result.budget_used_pct,
        "budget_alert": result.budget_alert,
        "truncated": result.truncated,
        "approval_required": True,
    }
