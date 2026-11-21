"""The agency's own services on the client's documents (AA-1147; S285 decisions T18 and B10; crm-083, settings crm-084).

An agency sells four things of its own (:mod:`app.ledger.agency_items`: flight, visa, travel insurance, its fee). On
the proposal (page, e-mail text, PDF) and the invoice (printable page, PDF) they stand in their own group, "Services
provided by <agency>", with a printed disclaimer: the agency, not Adventure Asia, arranges them and answers for them.

**Bundling rule (T18).** Only a flight and the agency fee may be folded into the tour price (the agency may not want
the client to compare the ticket price); such an item stays in the total but shows no price of its own. Visa (money
passed through to the authority) and insurance (the client must see the premium and may refuse it) are always their
own priced line. The database holds the same rule (crm-083 ``option_bundled_chk``, ``booking_line_extras_chk``).

The two values the tech lead edits (S279 rule: never a code literal) are settings of the CRM registry:

- ``crm.agency_lines.due_lead_days``: how many days before a line's deadline its task opens (:func:`lead_days`,
  :mod:`app.ledger.agency_due`);
- ``crm.agency_lines.disclaimer``: the disclaimer, one text per language (:func:`disclaimer`).

Both are ``platform+tenant``: an administrator may give one agency its own value.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app import platform_settings as ps
from app.errors import Invalid

# The agency items that may be folded into the tour price (T18); visa and insurance never are.
BUNDLE_CATEGORIES = ("flight", "fee")
LOCALES = ("en", "vi")


def bundled(value: Any, category: str | None) -> bool:
    """The "fold into the tour price" flag of an agency item or line: 422 ``bundle_not_allowed`` for a visa or an
    insurance item (always its own priced line) or an item without a type."""
    if value is None or value is False:
        return False
    if value is not True:
        raise Invalid("invalid_bundled", "true or false", field="bundled")
    if category not in BUNDLE_CATEGORIES:
        raise Invalid(
            "bundle_not_allowed",
            "only a flight or the agency fee may be folded into the tour price; visa and travel insurance are always "
            "their own priced line",
            field="bundled",
            allowed=list(BUNDLE_CATEGORIES),
        )
    return True


async def lead_days(conn: Any, tenant_id: uuid.UUID | str) -> int:
    """Days before a line's deadline that its task opens (the agency's own value, else the platform's)."""
    return int(await ps.tenant_get(conn, ps.AGENCY_LINES_DUE_LEAD_DAYS, tenant_id))


def pick_text(value: Any, locale: str) -> str:
    """The text of one language from a ``{en, vi}`` setting value: the locale's, else English, else empty."""
    if not isinstance(value, dict):
        return ""
    lang = "vi" if str(locale).startswith("vi") else "en"
    text = value.get(lang) or value.get("en") or ""
    return " ".join(str(text).split()) if isinstance(text, str) else ""


async def disclaimer(conn: Any, tenant_id: uuid.UUID | str, locale: str) -> str:
    """The disclaimer printed under "Services provided by <agency>", in ``locale`` (en / vi)."""
    return pick_text(await ps.tenant_get(conn, ps.AGENCY_LINES_DISCLAIMER, tenant_id), locale)


async def disclaimers(conn: Any, tenant_id: uuid.UUID | str) -> dict[str, str]:
    """Both languages (``{en, vi}``) for a view that renders later in either."""
    value = await ps.tenant_get(conn, ps.AGENCY_LINES_DISCLAIMER, tenant_id)
    return {lang: pick_text(value, lang) for lang in LOCALES}


def group_item(title: str, category: str | None, sell: Decimal | None, is_bundled: bool) -> dict[str, Any]:
    """One row of the group: the price is left out when the item is folded into the tour price."""
    return {
        "title": title,
        "category": category,
        "bundled": bool(is_bundled),
        "amount": None if is_bundled or sell is None else str(Decimal(sell).quantize(Decimal("0.01"))),
    }
