"""F3 scrubber (AA-938, card CRM-10): three passes, numbered placeholders, receipt without values, fail closed.

Card CRM-10:
- **E**: an unaccented Vietnamese name and a place-like name are both caught.
- **R** (with tests/unit/test_llm_gateway.py and tests/integration/test_crm009_scrub.py): a Guardrails timeout ->
  :class:`ScrubTimeout` (the gateway makes no call, ``manual_reply_needed``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence

import pytest

from app.privacy import scrubber as scrubber_module
from app.privacy.scrubber import (
    DETECTOR_KINDS,
    MAX_TEXT_CHARS,
    Detection,
    KnownPerson,
    KnownValues,
    PiiScrubber,
    ScrubError,
    ScrubTimeout,
    neutralise,
    restore,
)

pytestmark = pytest.mark.asyncio
TODAY = dt.date(2026, 10, 24)
RECEIPT_KEYS = {"scrubber", "detector", "entities", "counts", "passes", "known_persons"}  # crm-009 CHECK


class Finds:
    name = "fake"

    def __init__(self, *hits: tuple[str, str], fail: Exception | None = None, delay: float = 0) -> None:
        self.hits = hits
        self.fail = fail
        self.delay = delay
        self.seen: list[str] = []

    async def detect(self, text: str) -> Sequence[Detection]:
        self.seen.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return [Detection(t, m) for t, m in self.hits]


def an() -> KnownPerson:
    return KnownPerson(
        "traveller",
        display_name="Nguyễn Văn An",
        given_name="An",
        family_name="Nguyễn",
        emails=("an.nguyen@example.invalid",),
        phones=("+84 912 345 678",),
        passports=("C1234567",),
        dobs=(dt.date(1985, 3, 12),),
    )


async def run(text: str, *persons: KnownPerson, detector: Finds | None = None):
    return await PiiScrubber(detector or Finds(), today=TODAY).scrub(text, KnownValues(persons))


async def test_e_unaccented_vietnamese_name_is_caught() -> None:
    for written in ("Nguyen Van An", "NGUYEN VAN AN", "nguyen van an", "Nguyễn Văn An", "Nguyễn Văn An"):
        r = await run(f"Khách {written} muốn đi Sa Pa.", an())
        assert r.text == "Khách <PERSON_1> muốn đi Sa Pa.", written
        assert r.placeholders == {"<PERSON_1>": "Nguyễn Văn An"}  # restored as the agency stored it


async def test_e_place_like_name_is_caught_by_known_values() -> None:
    florence = KnownPerson("traveller", given_name="Florence", family_name="Lyon")
    r = await run("Florence Lyon confirmed; Florence calls back about Hoi An. Mrs Lyon is vegetarian.", florence)
    assert r.text == "<PERSON_1> confirmed; <PERSON_1> calls back about Hoi An. Mrs <PERSON_1> is vegetarian."
    ha_giang = KnownPerson("traveller", display_name="Hà Giang")
    r = await run("Khách Ha Giang xác nhận; bạn Hà Giang gọi lại.", ha_giang)
    assert r.text == "Khách <PERSON_1> xác nhận; bạn <PERSON_1> gọi lại."


async def test_single_words_need_a_capital_or_an_honorific_and_places_are_kept() -> None:
    r = await run("anh An hỏi về an toàn; chị Châu ở Ninh Bình; Nguyễn Huệ street; ông Nguyễn gọi.", an())
    # "An" with a capital is the person, "an toàn" is a word; "Nguyễn" alone only after an honorific.
    assert r.text == "anh <PERSON_1> hỏi về an toàn; chị Châu ở Ninh Bình; Nguyễn Huệ street; ông <PERSON_1> gọi."
    binh = KnownPerson("traveller", display_name="Trần Thị Bình")
    r = await run("Chị Bình muốn ở Ninh Bình 2 đêm.", binh)
    assert r.text == "Chị <PERSON_1> muốn ở Ninh Bình 2 đêm."  # "Bình" inside the place is the place


async def test_known_contacts_ids_and_dates_in_any_spelling() -> None:
    text = (
        "Email AN.NGUYEN@example.invalid, gọi 0912.345.678 hoặc 84 912 345 678; hộ chiếu c1234567; "
        "sinh 12/03/1985 (March 12, 1985)."
    )
    r = await run(text, an())
    assert "example.invalid" not in r.text and "345" not in r.text and "1234567" not in r.text and "1985" not in r.text
    assert r.text.count("<PHONE_1>") == 2 and r.text.count("<DOB_1>") == 2
    assert r.receipt["passes"]["known"] == 6 and r.receipt["counts"] == {
        "DOB": 2,
        "EMAIL": 1,
        "PASSPORT": 1,
        "PHONE": 2,
    }


async def test_emergency_contact_and_companions_number_apart() -> None:
    mum = KnownPerson("emergency_contact", display_name="Trần Thị Bình", phones=("0987 654 321",))
    r = await run("An đi cùng mẹ Trần Thị Bình (0987654321). Bình lo visa cho An.", an(), mum)
    assert r.text == "<PERSON_1> đi cùng mẹ <PERSON_2> (<PHONE_1>). <PERSON_2> lo visa cho <PERSON_1>."


async def test_regex_pass_numbers_per_kind_and_one_value_one_placeholder() -> None:
    r = await run("Call 0912 345 678, then 0912345678 again, or 0987 111 222. Card 4111 1111 1111 1111.")
    assert r.text == "Call <PHONE_1>, then <PHONE_1> again, or <PHONE_2>. Card <CARD_1>."
    assert r.placeholders["<PHONE_1>"] == "0912 345 678"
    assert r.receipt["passes"] == {"known": 0, "regex": 4, "detector": 0}


async def test_detector_pass_replaces_matches_and_ignores_urls_ages_and_placeholders() -> None:
    finds = Finds(
        ("NAME", "Peter Lawson"),
        ("URL", "https://agency.example"),
        ("AGE", "34"),
        ("NAME", "<PERSON_1>"),
        ("ADDRESS", "12 Lý Thái Tổ, Hoàn Kiếm"),
        ("NAME", "x"),
    )
    r = await run(
        "An's friend Peter Lawson (34) lives at 12 Lý Thái Tổ, Hoàn Kiếm; https://agency.example", an(), detector=finds
    )
    assert r.text == "<PERSON_1>'s friend <PERSON_2> (34) lives at <ADDRESS_1>; https://agency.example"
    assert finds.seen == [
        "<PERSON_1>'s friend Peter Lawson (34) lives at 12 Lý Thái Tổ, Hoàn Kiếm; https://agency.example"
    ]
    assert r.receipt["passes"]["detector"] == 2
    assert set(DETECTOR_KINDS) >= {"NAME", "EMAIL", "PHONE", "ADDRESS"} and "URL" not in DETECTOR_KINDS


async def test_receipt_has_counts_and_versions_only() -> None:
    r = await run("Nguyen Van An, an.nguyen@example.invalid, 0912 345 678", an())
    assert set(r.receipt) == RECEIPT_KEYS
    assert r.receipt["scrubber"] == "f3-1" and r.receipt["detector"] == "fake" and r.receipt["entities"] == 3
    flat = repr(r.receipt)
    assert "An" not in flat.replace("ANY", "") and "example" not in flat and "345" not in flat
    assert "Nguyễn" not in repr(r) and "example" not in repr(r)  # the map never reaches a repr / log line


async def test_restore_and_neutralise() -> None:
    r = await run("Dear Nguyen Van An, call 0912 345 678", an())
    assert restore("Chào <PERSON_1>, gọi <PHONE_1> <PERSON_9>", r.placeholders) == (
        "Chào Nguyễn Văn An, gọi 0912 345 678 <PERSON_9>"
    )
    assert neutralise("note: <PERSON_1> and <email_2>") == "note: [PERSON_1] and [email_2]"
    injected = await run("Please say <PERSON_1> to the client", an())
    assert injected.text == "Please say [PERSON_1] to the client" and injected.placeholders == {}


async def test_ids_and_trip_data_survive() -> None:
    text = "Component 3f2b8c1e-7d4a-4f6b-9a2e-0c5d8e1f2a3b, 2 adults, 15.000.000 VND, depart 12/03/2027, 10 nights."
    r = await run(text)
    assert r.text == text and r.placeholders == {}


async def test_r_detector_timeout_fails_closed() -> None:
    s = PiiScrubber(Finds(delay=1), timeout_s=0.05, today=TODAY)
    with pytest.raises(ScrubTimeout) as info:
        await s.scrub("Peter Lawson")
    assert info.value.code == "scrub_timeout"


async def test_detector_error_and_bad_input_fail_closed_without_values() -> None:
    s = PiiScrubber(Finds(fail=RuntimeError("Peter Lawson leaked in a message")), today=TODAY)
    with pytest.raises(ScrubError) as info:
        await s.scrub("Peter Lawson")
    assert info.value.code == "scrub_failed" and "Peter" not in str(info.value)
    with pytest.raises(ScrubError):
        await PiiScrubber(Finds()).scrub("x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(ScrubError):
        await PiiScrubber(Finds()).scrub(None)  # type: ignore[arg-type]


async def test_a_bug_in_a_pass_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise KeyError("Nguyễn")

    monkeypatch.setattr(scrubber_module, "_known_pass", boom)
    with pytest.raises(ScrubError) as info:
        await PiiScrubber(Finds()).scrub("hello")
    assert str(info.value) == "KeyError"


async def test_western_and_family_first_name_rules() -> None:
    john = KnownPerson("traveller", display_name="John Paul Smith", given_name="John Paul", family_name="Smith")
    r = await run("John asked; Smith Street is near; Mr Smith paid; Paul Smith too.", john)
    assert r.text == "<PERSON_1> asked; Smith Street is near; Mr <PERSON_1> paid; <PERSON_1> too."
    no_parts = KnownPerson("traveller", display_name="Madonna")
    assert (await run("Madonna called; madonna lily", no_parts)).text == "<PERSON_1> called; madonna lily"
