"""AA-1126 (crm-076): which AI spend rules this CRM environment follows, the setting ``crm.spend.environment``
(dev | prod; CIS Platform > Settings, CRM tab, maker-checker, its guide in ``app/platform_settings/guides.json``).

- ``dev`` (the default, today's behaviour): every hard stop stays. The CRM app's daily AI budget (AI gateway > App
  profiles) stops the AI drafts of every agency (``app_budget_exhausted``), and a spend that cannot be read stops them
  too (``spend_unreadable``).
- ``prod`` (tech lead policy S283, pay per use): the app's daily budget only alerts and an unreadable spend lets the
  draft go on with a warning. An agency's own monthly AI budget (from its plan) always stops at its limit.

Read from the settings snapshot (never the database on the call path); any doubt answers ``dev``, the stricter rules.
"""

from __future__ import annotations

import logging

from app import platform_settings as ps

log = logging.getLogger(__name__)

DEV = "dev"
PROD = "prod"


def spend_environment() -> str:
    """``dev`` or ``prod``; ``dev`` when the setting cannot be read."""
    try:
        value = ps.get(ps.SPEND_ENVIRONMENT)
    except Exception as exc:  # noqa: BLE001 — never on the call path
        log.warning("crm_spend_environment_unreadable error=%s", type(exc).__name__)
        return DEV
    return PROD if str(value or "").strip().lower() == PROD else DEV


def is_prod() -> bool:
    return spend_environment() == PROD
