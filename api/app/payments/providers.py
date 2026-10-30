"""The agency's own payment providers (BYO, design v3 §6.3, D10 a): one interface, one implementation per provider.

The agency is always the merchant of record: money moves between the client and the agency's own account (its bank,
its cash desk, its own card terminal or provider account). No Adventure Asia account ever holds, receives or refunds
client money, so no provider here is ever configured with an AA account.

- ``manual`` (today): the agency receives and returns money outside the CRM and a member records it. Its account row
  (``crm.payment_provider_account``) carries the pay-by instructions printed on the agency's invoices (its own bank
  details, its office for cash). A refund through it records what the member says was returned.
- ``stripe`` (Connect Standard on the agency's own account) and ``vn_gateway`` are reserved names (the database knows
  them). They have no implementation yet: asking for one is 409 ``provider_not_available``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.errors import Conflict

KNOWN = ("manual", "stripe", "vn_gateway")


@dataclass(frozen=True)
class RefundResult:
    """What a provider answered for a refund: its own reference (the agency's for a manual refund)."""

    provider_ref: str | None


class PaymentProvider(Protocol):
    name: str
    #: The provider takes card payments through a pay link (C31 Checkout); manual does not.
    takes_cards: bool
    #: The account row names an account at the provider (``external_account_id``); manual has none.
    needs_external_account: bool

    async def refund(self, *, amount: Decimal, currency: str, original_ref: str | None,
                     reference: str | None) -> RefundResult: ...  # fmt: skip


class ManualProvider:
    """Money the agency moved itself; the CRM only records it."""

    name = "manual"
    takes_cards = False
    needs_external_account = False

    async def refund(
        self, *, amount: Decimal, currency: str, original_ref: str | None, reference: str | None
    ) -> RefundResult:
        # Nothing to call: the member returned the money (bank transfer, cash) and records it with their reference.
        return RefundResult(provider_ref=reference)


PROVIDERS: dict[str, PaymentProvider] = {"manual": ManualProvider()}


def provider(name: str) -> PaymentProvider:
    """The implementation of a provider, or 409 ``provider_not_available`` (a known name without code yet) /
    ``unknown_provider``."""
    impl = PROVIDERS.get(name)
    if impl is not None:
        return impl
    if name in KNOWN:
        raise Conflict(
            "provider_not_available",
            "this provider is not available yet: record payments by hand (bank transfer, cash) meanwhile",
        )
    raise Conflict("unknown_provider", ", ".join(KNOWN))


def available() -> list[dict[str, object]]:
    """Every provider name with whether it can be used today (the Settings page lists them)."""
    return [{"provider": name, "available": name in PROVIDERS} for name in KNOWN]
