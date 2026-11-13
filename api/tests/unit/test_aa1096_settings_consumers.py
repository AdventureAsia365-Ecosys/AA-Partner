"""AA-1096 L4: the CRM code that moved onto the admin-editable settings (crm-064) reads them at the time of use, so an
approved platform value takes effect within the reader's cache window with no deploy. Each test puts one approved
platform value into the reader's snapshot (as a refresh would) and checks the consumer sees it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
from app.errors import Invalid
from app.intake import booking_settings
from app.lifecycle import policy, rules, window
from app.llm import gateway
from app.llm.providers.bedrock import BedrockConverseProvider
from app.payments import reminders
from app.payments import service as payments
from app.platform_settings.reader import _Row
from app.proposals import pricing, retail
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000002a1")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ps.invalidate()
    ps.READER._snapshot = {}
    yield
    ps.invalidate()
    ps.READER._snapshot = None


def approved(key: str, value: Any) -> None:
    ps.READER._snapshot[(key, None)] = _Row(value=value, version=2)  # type: ignore[index]


def test_reminder_cadence() -> None:
    assert (reminders.every_days(), reminders.max_reminders()) == (3, 5)
    approved(ps.INVOICE_REMINDER_EVERY_DAYS, 7)
    approved(ps.INVOICE_REMINDER_MAX, 0)
    assert (reminders.every_days(), reminders.max_reminders()) == (7, 0)


def test_agency_defaults_without_a_settings_row() -> None:
    approved(ps.REFUND_THRESHOLD_DEFAULT, 2500)
    approved(ps.PRICE_CHECKS_DEFAULT, 90)
    approved(ps.AI_BUDGET_DEFAULT, 1.5)
    assert payments.default_threshold() == Decimal("2500")
    assert booking_settings.default_price_checks() == 90
    assert gateway.default_budget_usd() == Decimal("1.5")


@pytest.mark.asyncio
async def test_discount_and_trash_defaults_of_an_agency_without_a_row() -> None:
    approved(ps.DISCOUNT_LIMIT_DEFAULT, 15)
    approved(ps.TRASH_DAYS_DEFAULT, 14)
    approved(ps.ARCHIVE_MONTHS_DEFAULT, 48)
    ctx = tenant_ctx(ScriptedConn())
    assert (await pricing.settings(ctx)).discount_owner_limit_pct == Decimal("15")
    assert await window.trash_days(ctx) == 14
    assert await policy.settings(ctx) == {"trash_days": 14, "auto_archive_months": 48}


@pytest.mark.asyncio
async def test_the_owner_picks_inside_the_platform_range() -> None:
    approved(ps.TRASH_DAYS_MAX, 10)
    approved(ps.ARCHIVE_MONTHS_MIN, 24)
    assert rules.owner_ranges() == {"trash_days": (1, 10), "auto_archive_months": (24, 120)}
    ctx = tenant_ctx(
        ScriptedConn().on("INSERT INTO crm.tenant_settings", {"trash_days": 10, "auto_archive_months": 24})
    )
    with pytest.raises(Invalid, match="1 to 10 days"):
        await policy.update_settings(ctx, trash_days=20, auto_archive_months=None)
    with pytest.raises(Invalid, match="24 to 120 months"):
        await policy.update_settings(ctx, trash_days=None, auto_archive_months=12)
    assert await policy.update_settings(ctx, trash_days=10, auto_archive_months=24) == {
        "trash_days": 10,
        "auto_archive_months": 24,
    }


def test_price_check_bounds_and_markup_ceiling() -> None:
    approved(ps.PRICE_CHECKS_MIN, 20)
    approved(ps.PRICE_CHECKS_MAX, 300)
    assert booking_settings.price_checks_bounds() == (20, 300)
    approved(ps.MARKUP_MAX, 150)
    assert ps.decimal(ps.MARKUP_MAX) == Decimal("150")


@pytest.mark.asyncio
async def test_a_markup_above_the_platform_ceiling_is_refused() -> None:
    approved(ps.MARKUP_MAX, 150)
    ctx = tenant_ctx(ScriptedConn().on("SELECT retail_markup_pct FROM crm.tenant_settings", "{}"))
    with pytest.raises(Invalid, match="0 to 150"):
        await retail.update(ctx, {"hotel_room": 200})
    assert (await retail.read(ctx))["markup_max_pct"] == "150"


@pytest.mark.asyncio
async def test_an_agency_budget_never_passes_its_ceiling() -> None:
    conn = ScriptedConn().on("WHERE v.tenant_id = $1", [])
    ctx = tenant_ctx(conn)
    assert await gateway.agency_budget(ctx, {"ai_budget_usd": Decimal("1.25")}) == Decimal("1.25")
    assert await gateway.agency_budget(ctx, {"ai_budget_usd": Decimal("5")}) == Decimal("2")  # the platform ceiling
    ps.forget_tenant()
    own = ScriptedConn().on("WHERE v.tenant_id = $1", [{"key": ps.AI_BUDGET_MAX, "value_text": "8", "version": 1}])
    assert await gateway.agency_budget(tenant_ctx(own), {"ai_budget_usd": Decimal("5")}) == Decimal("5")


def test_the_draft_token_cap_is_read_at_each_call() -> None:
    provider = BedrockConverseProvider("anthropic.zz-model-v1:0", max_tokens=ps.limit(ps.AI_DRAFT_MAX_TOKENS))
    assert provider._request("hi", None)["inferenceConfig"]["maxTokens"] == 800
    approved(ps.AI_DRAFT_MAX_TOKENS, 400)
    assert provider._request("hi", None)["inferenceConfig"]["maxTokens"] == 400
    assert BedrockConverseProvider("anthropic.zz-model-v1:0", max_tokens=300).output_cap() == 300
