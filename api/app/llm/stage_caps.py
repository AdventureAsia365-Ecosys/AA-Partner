"""The output token cap of each CRM AI stage that has its own (AA-965, CRM-33b; AA-1096: admin-editable settings read at
each call). The gateway holds the cap, never the call site (lessons: "the token cap is held by the gateway");
``crm_draft_reply`` keeps ``crm.ai.draft_reply_max_tokens`` on the provider itself.
"""

from __future__ import annotations

from typing import Any

from app import platform_settings as ps

STAGE_OUTPUT_CAPS = {
    "crm_supplier_confirm": ps.AI_SUPPLIER_CONFIRM_MAX_TOKENS,
    "crm_notes_options": ps.AI_NOTES_OPTIONS_MAX_TOKENS,
}


def stage_output_cap(stage: str) -> int | None:
    """The output token cap of ``stage`` when it has its own setting, else None (the provider's own cap)."""
    key = STAGE_OUTPUT_CAPS.get(stage)
    return int(ps.get(key)) if key else None


async def complete(who: Any, stage: str, text: str, system: str | None) -> Any:
    """One provider attempt with the stage's own output cap when it has one (the call shape of CRM-33a otherwise)."""
    cap = stage_output_cap(stage)
    if cap is not None:
        return await who.complete(text, system=system, max_tokens=cap)
    return await (who.complete(text, system=system) if system else who.complete(text))
