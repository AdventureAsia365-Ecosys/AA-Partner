"""The traveller cancellation policy, pure rules (AA-1067, items crm-11 / crm-12 of the S275 study; crm-049). No
database: :mod:`app.cancel_policy.service` loads the rows and calls these.

- **Tiers**: "cancelled N days or more before departure -> X % of the trip total, at least M". A policy has 1..12
  tiers, one per day count, and always a 0-day tier (so every cancellation finds one; a trip already started counts
  as 0 days). :data:`DEFAULT_TIERS` is the AA starting table the owner edits (S275 proposal 3A to the business
  owner): 31 days or more 0 %, 15-30 days 25 %, 8-14 days 50 %, 7 days or fewer 100 %. A 0 % tier is a valid line.
- **Charge** (:func:`policy_charge`): the tier's percent of the total, rounded per currency
  (:func:`app.payments.schedule_plan.round_money`), raised to the tier's minimum when the trip is in the policy's
  currency, never above the total.
- **Decision** (:func:`check_decision`, :func:`final_charge`): the policy's charge as is; an override (an amount from 0
  to the total, with a reason); a waiver (0, with a reason). Without a policy the charge is 0 unless overridden.
- **Refunds** (:func:`allocate`): the charge is kept from the client's payments, the oldest first; each payment's
  refund is what is left of it (never below 0); the fee invoice bills only the charge not covered by money kept
  (:func:`invoice_due`).
- **Text** (:func:`policy_text`): the tier table in words (EN / VI), the ``{{cancellation.policy}}`` merge field of a
  booking terms version and the client's proposal page.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.errors import Invalid
from app.payments.schedule_plan import CENT, HUNDRED, round_money

MAX_TIERS = 12
MAX_DAYS = 730
MAX_NAME = 80
MAX_REASON = 500
MODES = ("policy", "override", "waive")
DEFAULT_NAME = "Standard cancellation policy"
DEFAULT_TIERS: tuple[tuple[int, str], ...] = ((31, "0"), (15, "25"), (8, "50"), (0, "100"))


@dataclass(frozen=True)
class Tier:
    days_before_min: int
    charge_pct: Decimal
    min_charge_amount: Decimal | None = None


@dataclass(frozen=True)
class Decision:
    mode: str  # policy | override | waive
    amount: Decimal | None = None
    reason: str | None = None


def _err(code: str, message: str, field: str) -> Invalid:
    return Invalid(code, message, field=field)


def _decimal(value: Any, field: str, code: str, message: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _err(code, message, field) from None
    if not out.is_finite() or out != out.quantize(CENT):
        raise _err(code, message, field)
    return out


def check_tiers(raw: Any) -> list[Tier]:
    """The tier table from the editor (``[{days_before_min, charge_pct, min_charge_amount}]``), sorted by days, most
    first (422 ``invalid_tiers`` with the field path)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TIERS:
        raise _err("invalid_tiers", f"1 to {MAX_TIERS} lines", "tiers")
    out: list[Tier] = []
    for n, item in enumerate(raw):
        where = f"tiers.{n}"
        if not isinstance(item, dict) or set(item) - {"days_before_min", "charge_pct", "min_charge_amount"}:
            raise _err("invalid_tiers", "days_before_min, charge_pct, min_charge_amount", where)
        days = item.get("days_before_min")
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS:
            raise _err("invalid_tiers", f"whole days from 0 to {MAX_DAYS}", f"{where}.days_before_min")
        pct = _decimal(item.get("charge_pct"), f"{where}.charge_pct", "invalid_tiers", "a percent from 0 to 100")
        if not Decimal(0) <= pct <= HUNDRED:
            raise _err("invalid_tiers", "a percent from 0 to 100", f"{where}.charge_pct")
        minimum = item.get("min_charge_amount")
        if minimum is not None:
            minimum = _decimal(minimum, f"{where}.min_charge_amount", "invalid_tiers", "an amount above 0")
            if minimum <= 0 or minimum > Decimal("99999999.99"):
                raise _err("invalid_tiers", "an amount above 0", f"{where}.min_charge_amount")
        out.append(Tier(days, pct, minimum))
    days_list = [t.days_before_min for t in out]
    if len(set(days_list)) != len(days_list):
        raise _err("invalid_tiers", "one line per number of days", "tiers")
    if 0 not in days_list:
        raise _err("invalid_tiers", "add a line from 0 days (a cancellation on the day, or after the start)", "tiers")
    return sorted(out, key=lambda t: -t.days_before_min)


def default_tiers() -> list[Tier]:
    return [Tier(days, Decimal(pct)) for days, pct in DEFAULT_TIERS]


def days_before(start: dt.date | None, today: dt.date) -> int | None:
    """Whole days from the agency's today to the trip's first day (negative once it started); None without a date."""
    return None if start is None else (start - today).days


def tier_for(days: int | None, tiers: list[Tier]) -> Tier:
    """The tier for a cancellation ``days`` before departure: the one with the most days not above it. No date or a
    started trip = 0 days (the strictest line)."""
    count = max(days or 0, 0)
    ordered = sorted(tiers, key=lambda t: -t.days_before_min)
    return next((t for t in ordered if t.days_before_min <= count), ordered[-1])


def policy_charge(
    total: Decimal, days: int | None, tiers: list[Tier], currency: str, policy_currency: str | None
) -> tuple[Tier, Decimal]:
    """(the tier, the charge) of a policy: percent of the total, the tier minimum in the policy currency, at most the
    total, never below 0."""
    tier = tier_for(days, tiers)
    total = Decimal(total).quantize(CENT)
    amount = round_money(total * tier.charge_pct / HUNDRED, currency)
    if tier.min_charge_amount is not None and policy_currency == currency:
        amount = max(amount, Decimal(tier.min_charge_amount).quantize(CENT))
    return tier, max(Decimal("0.00"), min(amount, total))


def check_decision(raw: Any) -> Decision:
    """``{mode: policy | override | waive, amount, reason}`` from the cancel dialog (absent = the policy's charge)."""
    if raw is None:
        return Decision("policy")
    if not isinstance(raw, dict) or set(raw) - {"mode", "amount", "reason"}:
        raise _err("invalid_charge", "mode, amount, reason", "charge")
    mode = raw.get("mode", "policy")
    if mode not in MODES:
        raise _err("invalid_charge", ", ".join(MODES), "charge.mode")
    if mode == "policy":
        return Decision("policy")
    reason = raw.get("reason")
    reason = " ".join(reason.split()) if isinstance(reason, str) else ""
    if not reason or len(reason) > MAX_REASON:
        raise _err("charge_reason_required", f"say why (1 to {MAX_REASON} characters)", "charge.reason")
    if mode == "waive":
        return Decision("waive", Decimal("0.00"), reason)
    amount = _decimal(raw.get("amount"), "charge.amount", "invalid_charge", "an amount, 0 or more, two decimals")
    if amount < 0:
        raise _err("invalid_charge", "an amount, 0 or more", "charge.amount")
    return Decision("override", amount, reason)


def final_charge(policy_amount: Decimal | None, decision: Decision, total: Decimal) -> tuple[str, Decimal]:
    """(the stored decision code, the charge). ``policy_amount`` None = no policy."""
    total = Decimal(total).quantize(CENT)
    if decision.mode == "waive":
        return "waived", Decimal("0.00")
    if decision.mode == "override":
        if decision.amount is None or decision.amount > total:
            raise _err("invalid_charge", "at most the trip total", "charge.amount")
        return "override", decision.amount
    if policy_amount is None:
        return "no_policy", Decimal("0.00")
    return "policy", policy_amount


def allocate(payments: list[tuple[str, Decimal]], charge: Decimal) -> dict[str, Decimal]:
    """The refund of each payment (id, what can still be asked back of it), oldest first in the list: the charge is
    kept from the earliest payments; the rest of each is refunded. Never below 0; the refunds add up to
    ``max(0, sum - charge)``."""
    left = max(Decimal(charge), Decimal(0))
    out: dict[str, Decimal] = {}
    for pid, refundable in payments:
        refundable = max(Decimal(refundable), Decimal(0))
        keep = min(refundable, left)
        left -= keep
        out[pid] = (refundable - keep).quantize(CENT)
    return out


def refund_due(paid: Decimal, charge: Decimal) -> Decimal:
    return max(Decimal(paid) - Decimal(charge), Decimal(0)).quantize(CENT)


def invoice_due(charge: Decimal, kept: Decimal) -> Decimal:
    """What the cancellation fee invoice bills: the charge not covered by money the agency keeps."""
    return max(Decimal(charge) - Decimal(kept), Decimal(0)).quantize(CENT)


def aa_fee_warnings(charge: Decimal, currency: str, fees: list[dict[str, Any] | None]) -> list[str]:
    """The cancel dialog's warning when the client is charged less than AA's own cancellation fees (the agency would
    refund below its AA cost). ``fees``: Booking's dry-run fee per AA booking (``{amount, currency}`` or None when
    unknown). A fee in another currency is not compared: ``aa_fee_other_currency`` says so."""
    known = [f for f in fees if f is not None and f.get("amount") is not None]
    out: list[str] = []
    same = [Decimal(str(f["amount"])) for f in known if f.get("currency") == currency]
    if len(same) < len(known):
        out.append("aa_fee_other_currency")
    if same and Decimal(charge) < sum(same, Decimal(0)):
        out.append("charge_below_aa_fee")
    return out


# ── Text ─────────────────────────────────────────────────────────────────────────────────────────

_TEXT = {
    "en": {
        "heading": "Cancellation policy: {name} (version {version})",
        "from": "{days} days or more before departure: {pct} % of the trip price",
        "range": "{low} to {high} days before departure: {pct} % of the trip price",
        "last": "Fewer than {days} days before departure, or after the trip starts: {pct} % of the trip price",
        "only": "Any cancellation: {pct} % of the trip price",
        "min": " (at least {amount})",
    },
    "vi": {
        "heading": "Chính sách huỷ: {name} (phiên bản {version})",
        "from": "Từ {days} ngày trở lên trước ngày khởi hành: {pct} % giá chuyến đi",
        "range": "Từ {low} đến {high} ngày trước ngày khởi hành: {pct} % giá chuyến đi",
        "last": "Dưới {days} ngày trước ngày khởi hành, hoặc sau khi chuyến đi bắt đầu: {pct} % giá chuyến đi",
        "only": "Mọi trường hợp huỷ: {pct} % giá chuyến đi",
        "min": " (tối thiểu {amount})",
    },
}


def pct_text(value: Decimal) -> str:
    """A percent without trailing zeros (10.00 -> "10", 12.50 -> "12.5")."""
    return f"{value.normalize():f}"


def policy_text(name: str, version: int, tiers: list[Tier], currency: str | None, locale: str = "en") -> str:
    """The tier table in words, most days first (``{{cancellation.policy}}``)."""
    s = _TEXT.get(locale, _TEXT["en"])
    ordered = sorted(tiers, key=lambda t: -t.days_before_min)
    lines = [s["heading"].format(name=name, version=version)]
    for n, tier in enumerate(ordered):
        pct = pct_text(tier.charge_pct)
        if len(ordered) == 1:
            text = s["only"].format(pct=pct)
        elif n == 0:
            text = s["from"].format(days=tier.days_before_min, pct=pct)
        elif tier.days_before_min == 0:
            text = s["last"].format(days=ordered[n - 1].days_before_min, pct=pct)
        else:
            text = s["range"].format(low=tier.days_before_min, high=ordered[n - 1].days_before_min - 1, pct=pct)
        if tier.min_charge_amount is not None and currency:
            text += s["min"].format(amount=f"{tier.min_charge_amount:,.2f} {currency}")
        lines.append(f"- {text}")
    return "\n".join(lines)


def tiers_view(tiers: list[Tier]) -> list[dict[str, Any]]:
    return [
        {
            "days_before_min": t.days_before_min,
            "charge_pct": pct_text(t.charge_pct),
            "min_charge_amount": None if t.min_charge_amount is None else str(t.min_charge_amount),
        }
        for t in sorted(tiers, key=lambda t: -t.days_before_min)
    ]
