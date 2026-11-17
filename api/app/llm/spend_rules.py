"""AA-1126 G3 / L4: what the CRM gateway does under the AI spend rules of this environment (``crm.spend.environment``,
:mod:`app.llm.spend_environment`; kept out of ``gateway.py``, which is at its code-health size).

- dev (the default, today's behaviour): the CRM app's daily AI budget refuses every agency (``app_budget_exhausted``)
  and a day or month spend that cannot be read refuses the call (``spend_unreadable``, fail closed).
- prod (tech lead policy S283): the app's daily budget only alerts and an unreadable spend lets the call go on; each
  is one error log line. An agency's own monthly budget stays hard in both.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.aa_llm import budget as llm_budget
from app.llm import spend_environment

log = logging.getLogger(__name__)


def app_cap_alert_only(reason: str | None) -> bool:
    """True when the app-wide daily budget's refusal (``cap_reached`` or ``spend_unreadable``) becomes an alert only."""
    if not spend_environment.is_prod():
        return False
    if reason == llm_budget.REASON_CAP:
        log.error("crm_llm_app_budget_alert_only reason=%s", reason)
    else:
        log.error("crm_llm_app_spend_unreadable_degraded reason=%s", reason)
    return True


def unreadable_month() -> Decimal | None:
    """The agency month's spend when it cannot be read: None (refuse, dev) or 0 (go on with an error line, prod)."""
    if not spend_environment.is_prod():
        return None
    log.error("crm_llm_spend_unreadable_degraded")
    return Decimal(0)
