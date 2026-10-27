"""AA-951 (card CRM-22): the text proposal renderers, snapshot style. One :class:`Snapshot` of a builder version
renders as e-mail and WhatsApp text, English and Vietnamese; each output is compared with its golden file in
``tests/unit/golden/`` (regenerate after a deliberate change: ``UPDATE_GOLDEN=1 pytest tests/unit/test_text_render.py``
and review the diff). Plus the parts: money and dates per locale, merge fields, the visa rule, the price reference."""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.proposals import text_render as tr
from app.proposals.prep_packs import sections

GOLDEN = Path(__file__).parent / "golden"
HASH = "ab12cd34ef56" + "0" * 52
PACK = (
    "## Entry and visa\n\nMost travellers need a visa on arrival. See "
    "[immigration.gov.np](https://www.immigration.gov.np).\n\n## Money\n\nThe currency is the **Nepalese rupee**. "
    "Pay by Visa card in Kathmandu."
)


def snap(**over: Any) -> tr.Snapshot:
    base: dict[str, Any] = dict(
        kind="builder", version=3, price_hash=HASH, agency_name="Sen Vang Travel", trip_title="Nepal family",
        currency="USD", total=Decimal("2480"), deposit=Decimal("744"), balance_due_on=dt.date(2026, 11, 1),
        pay_on_arrival_total=Decimal("180"), body="A gentle week in the hills.", starts_on=dt.date(2026, 12, 1),
        rooms=({"type": "family", "adults": 2, "children": 1},),
        days=(
            tr.Day(1, dt.date(2026, 12, 1), "Arrive", "Kathmandu", "+05:45", "We meet you at the airport.", (
                tr.Item("Lodging", "Homestay", alternatives=(
                    tr.Alternative("Riverside Lodge", Decimal("40"), True),
                    tr.Alternative("Old palace", Decimal("-15")),
                )),
            )),
            tr.Day(2, dt.date(2026, 12, 2), None, "Pokhara", "+05:45", None, (
                tr.Item("Dinner", "Thakali set", pay_on_arrival=True),
                tr.Item("Trek", "Poon Hill", on_request=True),
            )),
        ),
        before_you_go=tr.BeforeYouGo(
            "Bring warm layers.", (tr.Pack("Before you go: Nepal", tuple(sections(PACK))),), True
        ),
    )  # fmt: skip
    base.update(over)
    return tr.Snapshot(**base)


@pytest.mark.parametrize("fmt", tr.FORMATS)
@pytest.mark.parametrize("locale", tr.LOCALES)
def test_every_format_and_locale_matches_its_golden_file(fmt: str, locale: str) -> None:
    rendered = tr.render(snap(), fmt, locale)
    path = GOLDEN / f"text_proposal_{fmt}_{locale}.txt"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(rendered.body, encoding="utf-8")
    assert rendered.body == path.read_text(encoding="utf-8")
    assert rendered.price_hash == HASH and rendered.body.rstrip().endswith("ab12cd34ef56")
    assert (rendered.subject is not None) is (fmt == "email")


def test_one_snapshot_one_hash_and_the_same_facts_in_every_text() -> None:
    texts = [tr.render(snap(), f, loc) for f in tr.FORMATS for loc in tr.LOCALES]
    assert {t.price_hash for t in texts} == {HASH}
    for t in texts:
        assert "https://www.immigration.gov.np" in t.body
        assert "Most travellers need a visa" not in t.body  # visa = the official link only (Q1 2.3)
        assert "Visa card" in t.body  # a payment card is not a visa section
        assert "Riverside Lodge" in t.body and "Homestay" in t.body


def test_whatsapp_marks_headings_bold_and_email_does_not() -> None:
    assert "*Day 1 · Tue 1 Dec 2026 · Kathmandu (UTC+05:45)*" in tr.render(snap(), "whatsapp", "en").body
    assert "*" not in tr.render(snap(), "email", "en").body


def test_total_only_shows_no_difference() -> None:
    days = (tr.Day(1, None, None, None, None, None, (tr.Item("Lodging", "Homestay", alternatives=(
        tr.Alternative("Riverside Lodge", None),)),)),)  # fmt: skip
    body = tr.render(snap(days=days), "email", "en").body
    assert "Other choices: Riverside Lodge\n" in body and "+40" not in body
    assert "Day 1\n" in body  # no date, no place


def test_a_text_version_renders_its_text_and_the_link() -> None:
    s = snap(kind="text", days=(), before_you_go=None, rooms=(), body="Five days.", currency="VND",
             total=Decimal("25000000"), deposit=Decimal("25000000"), balance_due_on=None,
             pay_on_arrival_total=Decimal(0))  # fmt: skip
    r = tr.render(s, "email", "vi", link_url="https://go.example.test/c/p/tok")
    assert "Tổng cộng: 25.000.000 VND" in r.body and "Số còn lại" not in r.body
    assert "Xem chi tiết, chọn dịch vụ và chấp nhận tại đây: https://go.example.test/c/p/tok" in r.body
    assert "Trước khi đi" not in r.body and r.subject == "Đề xuất chuyến đi của bạn: Nepal family (phiên bản 3)"


def test_day_0_without_packs_keeps_the_reminders() -> None:
    body = tr.render(snap(before_you_go=tr.BeforeYouGo(None)), "email", "en").body
    assert "Before you go\n\nTravel insurance" in body and "Fitness" not in body


def test_a_visa_section_without_a_link_points_to_the_government_site() -> None:
    pack = tr.Pack("P", tuple(sections("## Visa\n\nYou need a visa.")))
    body = tr.render(snap(before_you_go=tr.BeforeYouGo(None, (pack,))), "whatsapp", "en").body
    assert "Visa: Check the official government website for your passport." in body and "You need" not in body


def test_money_and_dates_per_locale() -> None:
    assert tr.money(Decimal("1234.5"), "USD", "en") == "1,234.50 USD"
    assert tr.money(Decimal("1234.5"), "USD", "vi") == "1.234,50 USD"
    assert tr.money(Decimal("1234567"), "VND", "vi") == "1.234.567 VND"
    assert tr.money(Decimal("-3"), "EUR", "en") == "-3.00 EUR"
    assert tr.delta(Decimal("-15"), "USD", "en") == "-15.00 USD"
    assert tr.date(dt.date(2026, 12, 6), "vi") == "Chủ Nhật, 06/12/2026"
    assert tr.date(dt.date(2026, 12, 6), "en", weekday=False) == "6 Dec 2026"
    assert tr.reference(HASH) == "ab12cd34ef56"


def test_merge_fields() -> None:
    text, unfilled = tr.merge(
        "Hi {{traveller_first_name}},\n{{ trip_name }} by {{agency_name}}\n{{agent_name}}\n{{unknown}}",
        {"traveller_first_name": "Lan", "trip_name": "Ha Giang", "agency_name": "Sen Vang", "agent_name": None},
    )
    assert text == "Hi Lan,\nHa Giang by Sen Vang\n{{unknown}}" and unfilled == ("agent_name",)


def test_a_template_goes_around_the_proposal() -> None:
    r = tr.render(snap(), "email", "en", template="Hi {{traveller_first_name}},\n\n{{proposal}}\n\nBye",
                  values={"traveller_first_name": "Lan"})  # fmt: skip
    assert r.body.startswith("Hi Lan,\n\nYour trip proposal") and r.body.rstrip().endswith("Bye")
    r = tr.render(snap(), "email", "en", template="Hello {{agent_name}}!", values={})
    assert r.body.startswith("Hello !\n\nYour trip proposal") and r.unfilled == ("agent_name",)


def test_bad_format_or_locale() -> None:
    with pytest.raises(ValueError):
        tr.render(snap(), "sms", "en")
    with pytest.raises(ValueError):
        tr.render(snap(), "email", "fr")
