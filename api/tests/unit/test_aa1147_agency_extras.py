"""AA-1147 (crm-083): the bundling rule and the two settings of the agency's own services."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
from app.errors import Invalid
from app.ledger import agency_extras

TENANT = uuid.UUID("11111111-2222-4333-8444-555555555555")


@pytest.mark.parametrize("category", ["flight", "fee"])
def test_a_flight_or_fee_may_be_folded_into_the_tour_price(category: str) -> None:
    assert agency_extras.bundled(True, category) is True
    assert agency_extras.bundled(False, category) is False
    assert agency_extras.bundled(None, category) is False


@pytest.mark.parametrize("category", ["visa", "insurance", None])
def test_visa_and_insurance_are_always_their_own_priced_line(category: str | None) -> None:
    with pytest.raises(Invalid) as err:
        agency_extras.bundled(True, category)
    assert err.value.code == "bundle_not_allowed"
    assert agency_extras.bundled(False, category) is False


def test_bundled_takes_a_boolean_only() -> None:
    with pytest.raises(Invalid) as err:
        agency_extras.bundled("yes", "flight")
    assert err.value.code == "invalid_bundled"


def test_the_settings_defaults_are_registered() -> None:
    assert ps.get(ps.AGENCY_LINES_DUE_LEAD_DAYS) == 3
    text = ps.get(ps.AGENCY_LINES_DISCLAIMER)
    assert set(text) == {"en", "vi"} and all(len(v) > 40 for v in text.values())
    for key in (ps.AGENCY_LINES_DUE_LEAD_DAYS, ps.AGENCY_LINES_DISCLAIMER):
        assert ps.definition(key).scope_kind == "platform+tenant"


def test_pick_text_falls_back_to_english() -> None:
    value = {"en": "Arranged by the agency.  ", "vi": "Do đại lý sắp xếp."}
    assert agency_extras.pick_text(value, "vi") == "Do đại lý sắp xếp."
    assert agency_extras.pick_text(value, "vi-VN") == "Do đại lý sắp xếp."
    assert agency_extras.pick_text(value, "en-GB") == "Arranged by the agency."
    assert agency_extras.pick_text({"en": "Only English."}, "vi") == "Only English."
    assert agency_extras.pick_text("not an object", "en") == ""
    assert agency_extras.pick_text({"en": 5}, "en") == ""


@pytest.mark.asyncio
async def test_readers_use_the_agency_value(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []
    values = {ps.AGENCY_LINES_DUE_LEAD_DAYS: 7, ps.AGENCY_LINES_DISCLAIMER: {"en": "Own words.", "vi": "Lời riêng."}}

    async def fake(conn: Any, key: str, tenant_id: Any) -> Any:
        seen.append((key, tenant_id))
        return values[key]

    monkeypatch.setattr(ps, "tenant_get", fake)
    assert await agency_extras.lead_days(object(), TENANT) == 7
    assert await agency_extras.disclaimer(object(), TENANT, "vi") == "Lời riêng."
    assert await agency_extras.disclaimers(object(), TENANT) == {"en": "Own words.", "vi": "Lời riêng."}
    assert {k for k, _ in seen} == set(values) and {t for _, t in seen} == {TENANT}


def test_group_item_hides_the_price_of_a_bundled_item() -> None:
    assert agency_extras.group_item("Return flight", "flight", Decimal("270"), True) == {
        "title": "Return flight", "category": "flight", "bundled": True, "amount": None,
    }  # fmt: skip
    assert agency_extras.group_item("E-visa", "visa", Decimal("50"), False)["amount"] == "50.00"
    assert agency_extras.group_item("Older item", None, None, False)["amount"] is None
