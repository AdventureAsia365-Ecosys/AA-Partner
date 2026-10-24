"""Pass 2 regex detectors (AA-938, design v3 §4.5 step 2): what is found, and what must survive."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from app.observability.scrub import scrub_text
from app.privacy import patterns
from app.privacy.patterns import find, luhn_ok
from app.privacy.text import fold, fold_with_map, original_span

TODAY = dt.date(2026, 10, 24)
# 20,000 characters take about 0.2 s (0.5 s under coverage); the cubic version took hours. Generous for slow CI.
PERF_BUDGET_S = 3.0
# A synthetic 12-digit CCCD, built so no 12-digit literal sits in the file (the resource-id scanner reads one as an
# AWS account id).
CCCD = "001085" + "012345"


def kinds(text: str) -> list[tuple[str, str]]:
    return [(text[s.start : s.end], s.kind) for s in find(text, TODAY)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gọi 0912 345 678", [("0912 345 678", "PHONE")]),
        ("SĐT: 0912345678.", [("0912345678", "PHONE")]),
        ("+84 912 345 678 / +84912345678", [("+84 912 345 678", "PHONE"), ("+84912345678", "PHONE")]),
        ("84-912-345-678", [("84-912-345-678", "PHONE")]),
        ("landline (028) 3822 1234", [("(028) 3822 1234", "PHONE")]),
        ("02838221234", [("02838221234", "PHONE")]),
        ("US (415) 555-0132", [("(415) 555-0132", "PHONE")]),
        ("+44 7700 900123", [("+44 7700 900123", "PHONE")]),
        ("whatsapp 61412345678", [("61412345678", "PHONE")]),
        ("room 12 0912 345 678", [("0912 345 678", "PHONE")]),
        ("(0987654321)", [("0987654321", "PHONE")]),
        ("card 4111 1111 1111 1111", [("4111 1111 1111 1111", "CARD")]),
        ("amex 378282246310005", [("378282246310005", "CARD")]),
        ("CCCD 001 085 012 345", [("001 085 012 345", "NATIONAL_ID")]),
        (CCCD, [(CCCD, "NATIONAL_ID")]),
        ("CMND 123456789", [("123456789", "NATIONAL_ID")]),
        ("passport C1234567", [("C1234567", "PASSPORT")]),
        ("passport no: 123456789", [("123456789", "PASSPORT")]),
        ("hộ chiếu số b7654321", [("b7654321", "PASSPORT")]),
        ("mã OTP 482913", [("482913", "OTP")]),
        ("your verification code is 7391", [("7391", "OTP")]),
        ("DOB 12/03/1985", [("12/03/1985", "DOB")]),
        ("ngày sinh: ngày 5 tháng 7 năm 1990", [("ngày 5 tháng 7 năm 1990", "DOB")]),
        ("born March 12, 1985", [("March 12, 1985", "DOB")]),
        ("born 3rd of April 1979", [("3rd of April 1979", "DOB")]),
        ("date of birth: 1992-11-30", [("1992-11-30", "DOB")]),
        ("sinh năm 1985", [("1985", "DOB")]),
        ("DOB 12/03/85", [("12/03/85", "DOB")]),
        ("we met on 14/02/2019", [("14/02/2019", "DOB")]),  # any full date 2+ years back: over-scrub accepted
        ("mail Hoa.Nguyen@Example.test now", [("Hoa.Nguyen@Example.test", "EMAIL")]),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc", [("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc", "SECRET")]),
    ],
)
def test_found(text: str, expected: list[tuple[str, str]]) -> None:
    assert kinds(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Tour depart 12/03/2027, return 22/03/2027, 10 nights",
        "2 adults, 1 child (7), budget 15.000.000 VND or USD 2,400",
        "component 550e8400-e29b-41d4-a716-012345678901 and 00000000-0000-4000-8000-000000000001",
        "<PHONE_1> and <PERSON_12>",
        "timestamp 2026-10-24T04:12:26.798436Z duration_ms 1234",
        "https://adventure.example/tours/hoi-an?lang=vi&page=2",
        "order 1234567890123",  # 13 digits, not Luhn
        "year 2027, 3 days, 4-5 hours",
        "trip 12/03/2027",
    ],
)
def test_survives(text: str) -> None:
    assert kinds(text) == []


def test_cue_stays_in_its_clause() -> None:
    assert kinds("DOB 12/03/1985, trip 12/03/2027") == [("12/03/1985", "DOB")]
    assert kinds("Tel: 0912345678 and code 4821") == [("0912345678", "PHONE"), ("4821", "OTP")]


@pytest.mark.parametrize(
    "url",
    [
        "https://b.s3.example/doc.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc",
        "https://agency.example/reset?token=xyz",
        "https://go-dev.example/c/p/AbCdEf",
        "https://pay.example/checkout/a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
        "https://x.example/p?ref=abcdefghijklmnopqrstuvwxyz0123",
    ],
)
def test_signed_links(url: str) -> None:
    assert kinds(f"open {url}.") == [(url, "LINK")]


def test_luhn() -> None:
    assert luhn_ok("4111111111111111") and luhn_ok("378282246310005") and not luhn_ok("4111111111111112")


def test_fold_maps_back_to_the_original() -> None:
    text = "Nguyễn ĐỨC Hoà"
    folded, index = fold_with_map(text)
    assert folded == "nguyen duc hoa" and fold("Đà Lạt") == "da lat"
    start = folded.index("duc")
    assert text[slice(*original_span(index, start, start + 3))] == "ĐỨC"


# ── Review AA-938: cost stays linear (passes 1-2 and the log / Sentry path run on every string) ────────────────────


@pytest.mark.parametrize(
    "shape",
    [
        "1 ",
        "1-",
        "1.",
        "(1)",
        "+1 ",
        "0912 345 678 ",
        "Prices: 10 11 12 13 14 15 16 17 18 19 ",
        "a",
        "a.",
    ],
)
def test_find_is_linear_on_adversarial_text(shape: str) -> None:
    # Before the fix a 2,400-char digit list took 1.6 s (cubic); 20,000 characters would have taken hours.
    text = (shape * (20_000 // len(shape) + 1))[:20_000]
    for scan in (lambda: patterns.find(text, TODAY), lambda: scrub_text(text)):
        started = time.perf_counter()
        scan()
        assert time.perf_counter() - started < PERF_BUDGET_S, shape
