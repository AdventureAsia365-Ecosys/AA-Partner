"""CRM-16 (AA-944): the agency-line ledger and the supplier directory (design v3 M6 non-AA, M15, R17, R18).

- :mod:`app.ledger.suppliers`: the directory (``crm.agency_supplier``), Adventure Asia as a built-in row
  (``crm.ensure_aa_supplier()``), contact fields in the vault, revealed one field at a time with a reason.
- :mod:`app.ledger.lines`: a trip's booking lines (``crm.booking_line``), their money (``crm.booking_line_money``
  through the crm-014 definer functions), confirm / cancel with the agency's validation level (R18), the A-11 ledger
  and the ``waiting_on`` hook (``supplier`` while an agency line is not confirmed; AA-943 contract).
- :mod:`app.ledger.fx`: rate resolution (``crm.fx_resolve``, R17 §3.1), the owner's overrides, the payment settings
  (base currency, validation level, FX mode).

The database holds the rules (crm-014): this package words the refusals. :func:`refusal` maps the crm-014 SQLSTATEs to
service errors; a caller runs the statement inside a savepoint so the request can still answer.
"""

from __future__ import annotations

import asyncpg

from app.errors import Conflict, Forbidden, NotFound, ServiceError

# crm-014 SQLSTATE -> (error class, code). CRM23 / CRM25 carry details after the code ("line_incomplete:cost,sell").
REFUSALS: dict[str, tuple[type[ServiceError], str]] = {
    "CRM13": (NotFound, "line_not_found"),
    "CRM22": (Conflict, "aa_supplier_builtin"),
    "CRM23": (Conflict, "line_incomplete"),
    "CRM24": (Forbidden, "aa_line_read_only"),
    "CRM25": (Conflict, "fx_rate_missing"),
    "CRM26": (Conflict, "line_not_editable"),
    "CRM27": (Conflict, "supplier_in_use"),
}
DUPLICATE_CONSTRAINT = "booking_line_conf_uq"


def _detail(exc: asyncpg.PostgresError, code: str) -> str:
    """The text after ``<code>:`` in the database message ("crm: line_incomplete:cost,sell" -> "cost,sell")."""
    text = str(exc)
    marker = f"{code}:"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split(" ", 1)[0].strip()


def refusal(exc: asyncpg.PostgresError) -> ServiceError | None:
    """The service error for a crm-014 refusal, or None (the caller re-raises)."""
    if isinstance(exc, asyncpg.UniqueViolationError) and exc.constraint_name == DUPLICATE_CONSTRAINT:
        return Conflict("duplicate_confirmation_no", "this supplier already has a confirmed line with that number")
    if isinstance(exc, asyncpg.InsufficientPrivilegeError) and "margin_forbidden" in str(exc):
        return Forbidden("role_forbidden", "only an owner or an agent sees and edits line money")
    mapped = REFUSALS.get(getattr(exc, "sqlstate", "") or "")
    if mapped is None:
        return None
    cls, code = mapped
    if code == "line_incomplete":
        missing = [m for m in _detail(exc, code).split(",") if m]
        return cls(code, "fill these in before confirming", missing=missing)
    if code == "fx_rate_missing":
        return cls(code, "no exchange rate for this currency in the last 30 days", currency=_detail(exc, code))
    return cls(code)
