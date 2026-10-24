"""F3 scrubber (AA-938, card CRM-10): three passes, numbered placeholders, receipt without values, fail closed.

Card CRM-10:
- **E**: an unaccented Vietnamese name and a place-like name are both caught.
- **R** (with tests/unit/test_llm_gateway.py and tests/integration/test_crm009_scrub.py): a Guardrails timeout ->
  :class:`ScrubTimeout` (the gateway makes no call, ``manual_reply_needed``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
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
# 20,000 characters take about 0.2 s (0.5 s under coverage); the cubic version took hours. Generous for slow CI.
PERF_BUDGET_S = 3.0
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
        ("NAME", "--"),
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
    assert (
        r.receipt["scrubber"] == scrubber_module.VERSION == "f3-3"
        and r.receipt["detector"] == "fake"
        and r.receipt["entities"] == 3
    )
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


async def test_detector_finding_with_placeholders_replaces_every_other_fragment() -> None:
    # Review AA-938: the address around a known name used to be dropped (it contains "<PERSON_1>") and reach the model.
    finds = Finds(("ADDRESS", "12 <PERSON_1> street, Ward 5, Da Nang"))
    r = await run("Deliver to 12 Nguyen Van An street, Ward 5, Da Nang tomorrow.", an(), detector=finds)
    assert r.text == "Deliver to <ADDRESS_1> <PERSON_1> <ADDRESS_2> tomorrow."
    assert r.placeholders == {
        "<PERSON_1>": "Nguyễn Văn An",
        "<ADDRESS_1>": "12",
        "<ADDRESS_2>": "street, Ward 5, Da Nang",
    }
    assert "Ward" not in r.text and "street" not in r.text
    assert restore(r.text, r.placeholders) == "Deliver to 12 Nguyễn Văn An street, Ward 5, Da Nang tomorrow."


async def test_detector_finding_across_lines_or_case_is_still_replaced() -> None:
    # Review AA-938: "John Smith" on "John\nSmith" was not found verbatim and the name reached the model.
    r = await run("Guest: John\nSmith, room 4", detector=Finds(("NAME", "John Smith")))
    assert r.text == "Guest: <PERSON_1>, room 4"
    assert r.placeholders == {"<PERSON_1>": "John\nSmith"}
    r = await run("guest JOHN SMITH and Lê Văn Tám", detector=Finds(("NAME", "John Smith"), ("NAME", "Le Van Tam")))
    assert r.text == "guest <PERSON_1> and <PERSON_2>"
    r = await run("A called; a note", detector=Finds(("NAME", "A")))  # one letter: replaced, never skipped
    assert r.text == "<PERSON_1> called; a note"


async def test_detector_finding_that_cannot_be_placed_fails_closed() -> None:
    for hits in ((("NAME", "Peter Lawson"),), (("NAME", ""),), (("ADDRESS", "   "),)):
        with pytest.raises(ScrubError) as info:
            await run("Text without that name.", detector=Finds(*hits))
        assert info.value.code == "scrub_failed" and "Peter" not in str(info.value)
    # Unmapped kinds and findings made only of placeholders have nothing left to remove: no error.
    r = await run("An, 34, https://agency.example", an(), detector=Finds(("AGE", "99"), ("NAME", "<PERSON_1>")))
    assert r.text == "<PERSON_1>, 34, https://agency.example"


async def test_overlapping_detector_findings_merge_instead_of_dropping_one() -> None:
    finds = Finds(("NAME", "Mary Smith"), ("ADDRESS", "Smith Street 5"))
    r = await run("Ask Mary Smith Street 5 tonight", detector=finds)
    assert r.text == "Ask <ADDRESS_1> tonight"
    assert r.placeholders == {"<ADDRESS_1>": "Mary Smith Street 5"}


async def test_lower_case_given_name_after_an_honorific_is_the_person() -> None:
    hoa = KnownPerson("traveller", display_name="Nguyễn Thị Hoa")
    r = await run("chị hoa hỏi giá; anh an gọi lại; hoa sen nở; an toàn", hoa, an())
    assert r.text == "chị <PERSON_1> hỏi giá; anh <PERSON_2> gọi lại; hoa sen nở; an toàn"


async def test_western_and_family_first_name_rules() -> None:
    john = KnownPerson("traveller", display_name="John Paul Smith", given_name="John Paul", family_name="Smith")
    r = await run("John asked; Smith Street is near; Mr Smith paid; Paul Smith too.", john)
    assert r.text == "<PERSON_1> asked; Smith Street is near; Mr <PERSON_1> paid; <PERSON_1> too."
    no_parts = KnownPerson("traveller", display_name="Madonna")
    assert (await run("Madonna called; madonna lily", no_parts)).text == "<PERSON_1> called; madonna lily"


@pytest.mark.parametrize("shape", ["1 ", "0912 345 678 ", "Prices: 10 11 12 13 ", "Nguyen Van An chị an Ninh Bình "])
async def test_scrub_of_a_large_prompt_is_fast_and_off_the_event_loop(shape: str) -> None:
    # Review AA-938: a 2,400-char digit list took 1.6 s and 24,000 chars would have taken hours, on the event loop.
    text = (shape * (20_000 // len(shape) + 1))[:20_000]
    started = time.perf_counter()
    await PiiScrubber(Finds(), timeout_s=5, today=TODAY).scrub(text, KnownValues((an(),)))
    assert time.perf_counter() - started < PERF_BUDGET_S, shape


async def test_local_passes_share_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(text, known, reg):  # noqa: ANN001, ANN202
        time.sleep(0.3)
        return text

    monkeypatch.setattr(scrubber_module, "_known_pass", slow)
    finds = Finds()
    with pytest.raises(ScrubTimeout):
        await PiiScrubber(finds, timeout_s=0.05, today=TODAY).scrub("Peter Lawson")
    assert finds.seen == []  # the detector is never reached


# ── Option C (AA-938, S249): bare destinations typed <PLACE_n>, still masked ─────────────────────────


async def test_detector_address_that_is_one_destination_becomes_a_typed_place() -> None:
    # The live corpus run: Guardrails labels bare destinations ADDRESS (over-scrub 28.5 %). They stay masked, typed.
    finds = Finds(("ADDRESS", "Hà Giang"), ("ADDRESS", "Hoi An"), ("ADDRESS", "Hạ Long"), ("ADDRESS", "Huế"))
    r = await run("Tour Hà Giang 4 ngày, rồi Hoi An, Hạ Long và Huế; lại Hà Giang.", detector=finds)
    assert r.text == "Tour <PLACE_1> 4 ngày, rồi <PLACE_2>, <PLACE_3> và <PLACE_4>; lại <PLACE_1>."
    assert r.placeholders == {
        "<PLACE_1>": "Hà Giang",
        "<PLACE_2>": "Hoi An",
        "<PLACE_3>": "Hạ Long",
        "<PLACE_4>": "Huế",
    }
    assert r.receipt["counts"] == {"PLACE": 5} and r.receipt["passes"]["detector"] == 5
    reply = "Lịch <PLACE_1> -> <PLACE_2> -> <PLACE_3> ổn ạ."
    assert restore(reply, r.placeholders) == "Lịch Hà Giang -> Hoi An -> Hạ Long ổn ạ."


async def test_a_place_plus_more_keeps_its_address_kind() -> None:
    finds = Finds(
        ("ADDRESS", "45 Hải Phòng, Đà Nẵng"), ("ADDRESS", "Ninh Bình + Hải Dương"), ("ADDRESS", "Ha Long Bay 3")
    )
    r = await run("Nhà 45 Hải Phòng, Đà Nẵng; phòng Ninh Bình + Hải Dương; Ha Long Bay 3.", detector=finds)
    assert r.text == "Nhà <ADDRESS_1>; phòng <ADDRESS_2>; <ADDRESS_3>."


async def test_a_name_finding_equal_to_a_place_stays_a_person() -> None:
    # A person named like a place is never hinted as a place; a tie on the same words keeps today's kind (ADDRESS).
    r = await run("Contact person: Hà Nam.", detector=Finds(("NAME", "Hà Nam")))
    assert r.text == "Contact person: <PERSON_1>."
    r = await run("Guest Mai Châu.", detector=Finds(("NAME", "Mai Châu"), ("ADDRESS", "Mai Châu")))
    assert r.text == "Guest <ADDRESS_1>."
    # An address that swallows a place-typed finding is one ADDRESS, never a PLACE.
    r = await run("Ship to 12 Hội An road.", detector=Finds(("ADDRESS", "Hội An"), ("ADDRESS", "12 Hội An road")))
    assert r.text == "Ship to <ADDRESS_1>."
    # A known companion called like a place is pass 1's: PERSON, and pass 3 only ever sees the placeholder.
    hg = KnownPerson("companion", display_name="Hà Giang")
    finds = Finds()
    r = await run("Đi cùng Hà Giang tới Hà Giang", hg, detector=finds)
    assert r.text == "Đi cùng <PERSON_1> tới <PERSON_1>" and finds.seen == [r.text]


# Reviewer's live probe (ApplyGuardrail, acc2, S249): people named like places. Every name must stay masked; NAME
# findings stay PERSON; only ADDRESS findings that are exactly one place become PLACE.
LIVE_PROBE = [
    (
        "Khách Kim Liên (vợ anh Tuấn) muốn đổi phòng.",
        [("NAME", "Kim Liên"), ("NAME", "Tuấn")],
        "Khách <PERSON_1> (vợ anh <PERSON_2>) muốn đổi phòng.",
    ),
    ("Liên hệ chị Hải Dương để nhận vé.", [("NAME", "Hải Dương")], "Liên hệ chị <PERSON_1> để nhận vé."),
    ("Our driver Long An will meet you.", [("NAME", "Long An")], "Our driver <PERSON_1> will meet you."),
    (
        "Travellers: Mai Châu and Thái Bình.",
        [("ADDRESS", "Mai Châu"), ("ADDRESS", "Thái Bình")],
        "Travellers: <PLACE_1> and <PLACE_2>.",
    ),
    (
        "Giao vé tới số 12 phố Kim Liên, Đống Đa.",
        [("ADDRESS", "số 12 phố Kim Liên, Đống Đa")],
        "Giao vé tới <ADDRESS_1>.",
    ),
    (
        "Danh sách khách: Hà Giang, Kim Liên, Hòa Bình.",
        [("ADDRESS", "Hà Giang"), ("NAME", "Kim Liên"), ("ADDRESS", "Hòa Bình")],
        "Danh sách khách: <PLACE_1>, <PERSON_1>, <PLACE_2>.",
    ),
    (
        "Passengers: Mrs Hà Giang, Mr Long An.",
        [("NAME", "Hà Giang"), ("NAME", "Long An")],
        "Passengers: Mrs <PERSON_1>, Mr <PERSON_2>.",
    ),
    (
        "Room 1: Ninh Bình + Hải Dương. Room 2: Thanh Hóa.",
        [("ADDRESS", "Ninh Bình + Hải Dương"), ("ADDRESS", "Thanh Hóa")],
        "Room 1: <ADDRESS_1>. Room 2: <PLACE_1>.",
    ),
    (
        "Khách Mai Châu và chồng là Thái Bình đi tour Sa Pa.",
        [("ADDRESS", "Mai Châu"), ("NAME", "Thái Bình"), ("ADDRESS", "Sa Pa")],
        "Khách <PLACE_1> và chồng là <PERSON_1> đi tour <PLACE_2>.",
    ),
    (
        "Traveller Hà Giang wants to visit Hạ Long.",
        [("NAME", "Hà Giang"), ("NAME", "Hạ Long")],
        "Traveller <PERSON_1> wants to visit <PERSON_2>.",
    ),
]


@pytest.mark.parametrize(("text", "hits", "expected"), LIVE_PROBE)
async def test_people_named_like_places_stay_masked(text: str, hits: list, expected: str) -> None:
    r = await run(text, detector=Finds(*hits))
    assert r.text == expected
    for _, value in hits:
        assert value not in r.text  # nothing that was masked before option C is readable now
    assert restore(r.text, r.placeholders) == text  # every placeholder, PLACE included, restores


async def test_is_place_matches_exactly_one_destination_folded() -> None:
    from app.privacy.places import PLACE_PHRASES, is_place

    for yes in ("Hà Giang", "ha giang", "HOI AN", "Hội An,", " (Huế) ", "Ha Long Bay", "Mekong Delta", "Sa-Pa"):
        assert is_place(yes), yes
    for no in ("45 Hải Phòng, Đà Nẵng", "Ninh Bình + Hải Dương", "Hà", "Giang", "Northern Loop", "Hà Giang 4", ""):
        assert not is_place(no), no
    assert "hue" not in PLACE_PHRASES and "sapa" not in PLACE_PHRASES  # typing-only: pass 1 still sees "chị Huế"


# ── Option B (AA-938, S250, f3-3): the trip's own non-name-like destinations stay in clear ───────────

VN = ("vietnam",)
NAME_LIKE_PLACES = (
    "Mai Châu",
    "Thái Bình",
    "Hà Giang",
    "Hòa Bình",
    "Long An",
    "Thanh Hóa",
    "Kim Liên",
    "Hải Dương",
    "Phú Quốc",
    "Huế",
    "Ninh Bình",
    "Ninh Binh",
    "Ha Long",
    "Ha Long Bay",
    "Hà Nam",
    "Cao Bằng",
    "Lâm Đồng",
    "An Giang",
    "Kiên Giang",
    "Phan Thiết",
)


async def scrub_for(text: str, destinations: tuple[str, ...], *hits: tuple[str, str], persons=()):  # noqa: ANN001, ANN201
    known = KnownValues(tuple(persons), destinations=destinations)
    return await PiiScrubber(Finds(*hits), today=TODAY).scrub(text, known)


async def test_option_b_a_trip_destination_tagged_name_or_address_stays_in_clear() -> None:
    hits = (("NAME", "Hạ Long"), ("NAME", "Hoi An"), ("ADDRESS", "Sa Pa"), ("ADDRESS", "Đà Lạt"))
    r = await scrub_for("Đi Hạ Long rồi Hoi An, Sa Pa và Đà Lạt; lại Hạ Long.", VN, *hits)
    assert r.text == "Đi Hạ Long rồi Hoi An, Sa Pa và Đà Lạt; lại Hạ Long."
    assert r.placeholders == {}
    assert r.receipt["counts"] == {"PLACE_KEPT": 5} and r.receipt["entities"] == 0
    assert r.receipt["passes"]["detector"] == 0 and r.receipt["scrubber"] == "f3-3"
    assert set(r.receipt) == RECEIPT_KEYS  # crm-009 CHECK: PLACE_KEPT is a counts key with a number value
    # A person in the same text is still masked; the kept count stays out of "entities".
    r = await scrub_for("Peter Lawson muốn đi Sa Pa.", VN, ("NAME", "Peter Lawson"), ("NAME", "Sa Pa"))
    assert r.text == "<PERSON_1> muốn đi Sa Pa." and r.receipt["entities"] == 1
    assert r.receipt["counts"] == {"PERSON": 1, "PLACE_KEPT": 1}


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("Đi Hạ Long rồi Hoi An.", [("NAME", "Hạ Long"), ("ADDRESS", "Hoi An")]),
        ("Travellers: Mai Châu and Thái Bình.", [("ADDRESS", "Mai Châu"), ("ADDRESS", "Thái Bình")]),
        ("Guest Mai Châu.", [("NAME", "Mai Châu"), ("ADDRESS", "Mai Châu")]),
        ("Ship to 12 Hội An road.", [("ADDRESS", "Hội An"), ("ADDRESS", "12 Hội An road")]),
        ("Liên hệ chị Sa Pa, đi Sa Pa.", [("NAME", "Sa Pa")]),
    ],
)
async def test_option_b_without_destinations_is_f3_2_byte_for_byte(text: str, hits: list) -> None:
    f32 = await run(text, detector=Finds(*hits))
    none = await scrub_for(text, (), *hits)
    assert (none.text, none.placeholders, none.receipt) == (f32.text, f32.placeholders, f32.receipt)
    assert "PLACE_KEPT" not in none.receipt["counts"]


async def test_option_b_a_place_outside_the_trip_countries_is_masked_as_in_f3_2() -> None:
    hits = (("NAME", "Siem Reap"), ("ADDRESS", "Hội An"))
    r = await scrub_for("Siem Reap then Hội An.", ("laos",), *hits)
    assert r.text == "<PERSON_1> then <PLACE_1>."
    r = await scrub_for("Siem Reap then Hội An.", ("vietnam", "cambodia"), *hits)
    assert r.text == "Siem Reap then Hội An."
    r = await scrub_for("Siem Reap then Hội An.", ("not_sure",), *hits)
    assert r.text == "<PERSON_1> then <PLACE_1>."


@pytest.mark.parametrize("place", NAME_LIKE_PLACES)
async def test_option_b_a_name_like_place_is_never_kept(place: str) -> None:
    for kind in ("NAME", "ADDRESS"):
        r = await scrub_for(f"Đi cùng {place} tới {place}.", VN, (kind, place))
        assert place not in r.text, (place, kind)
        assert "PLACE_KEPT" not in r.receipt["counts"]


async def test_option_b_marks_decide_the_reading() -> None:
    # "Hạ Long" (the bay) is kept; "Hà Long" (a family name and a given name) and unmarked "Ha Long" are not.
    r = await scrub_for("Hạ Long, Hà Long, Ha Long.", VN, ("NAME", "Hạ Long"), ("NAME", "Hà Long"), ("NAME", "Ha Long"))
    assert r.text == "Hạ Long, <PERSON_1>, <PERSON_1>."
    # A folded locate (the detector's spelling differs from the text) keeps nothing the text does not spell exactly.
    r = await scrub_for("Khách tên Hà Long.", VN, ("NAME", "Hạ Long"))
    assert r.text == "Khách tên <PERSON_1>."


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Liên hệ chị Sa Pa để nhận vé.", ("NAME", "Sa Pa")),
        ("Please call Mrs Hoi An tomorrow.", ("NAME", "Hoi An")),
        ("Mr. Đà Lạt will join.", ("NAME", "Đà Lạt")),
        ("Our guest Hội An arrives Monday.", ("NAME", "Hội An")),
        ("Travellers: Đà Lạt and Hannah.", ("ADDRESS", "Đà Lạt")),
        ("Khách Hạ Long muốn đổi phòng.", ("NAME", "Hạ Long")),
        ("anh Sapa gọi lại.", ("NAME", "Sapa")),
        # S250 review, live on acc2 ApplyGuardrail: a cue with no space after it, or in brackets, still counts.
        ("Mr.Hội An confirmed the booking.", ("NAME", "Hội An")),
        ("Mrs.Sa Pa will pay the deposit.", ("NAME", "Sa Pa")),
        ("(Mrs) Hội An will join.", ("NAME", "Hội An")),
        ("Ms:Đà Lạt, room 2.", ("NAME", "Đà Lạt")),
        ("Mrs (Hội An) will join.", ("NAME", "Hội An")),
        # S250 review, live: kin and naming words left a person in clear.
        ("My wife Hội An and I arrive Monday.", ("NAME", "Hội An")),
        ("Vợ tôi tên Hội An, đi cùng tôi.", ("NAME", "Hội An")),
        ("Name: Nha Trang, phone later.", ("NAME", "Nha Trang")),
        ("Dear Đà Lạt, thanks for the deposit.", ("NAME", "Đà Lạt")),
        ("Our guide Sa Pa meets you at 8.", ("NAME", "Sa Pa")),
        ("Con gái Hội An đi cùng.", ("NAME", "Hội An")),
        ("vo toi ten Hoi An", ("NAME", "Hoi An")),
    ],
)
async def test_option_b_after_an_honorific_or_a_person_word_stays_masked(text: str, hit: tuple[str, str]) -> None:
    r = await scrub_for(text, VN, hit)
    assert hit[1] not in r.text and "PLACE_KEPT" not in r.receipt["counts"]
    assert restore(r.text, r.placeholders) == text


@pytest.mark.parametrize(
    ("text", "hits", "gone"),
    [
        # S250 review: a name found in pieces. The family name and the destination come back apart.
        ("Khách Lê Hội An đã cọc", [("NAME", "Lê"), ("NAME", "Hội An")], ["Lê", "Hội An"]),
        # Only the destination is found: the family name, middle name or placeholder next to it still marks a name.
        ("Khách Lê Hội An đã cọc", [("NAME", "Hội An")], ["Hội An"]),
        ("Nguyễn Thị Nha Trang sẽ đến muộn.", [("NAME", "Nha Trang")], ["Nha Trang"]),
        ("Booked for Hội An Nguyễn and family.", [("NAME", "Hội An")], ["Hội An"]),
        ("Booked for Hoi An Nguyen and family.", [("NAME", "Hoi An")], ["Hoi An"]),
        ("lê hội an đã cọc", [("NAME", "hội an")], ["hội an"]),
        # Pieces touching a masked one are masked in turn (whitespace only between).
        ("Khách Lê Hội An Sa Pa đã cọc", [("NAME", "Lê"), ("NAME", "Hội An"), ("NAME", "Sa Pa")], ["Hội An", "Sa Pa"]),
        ("Room for Peter Sa Pa", [("NAME", "Peter"), ("NAME", "Sa Pa")], ["Peter", "Sa Pa"]),
    ],
)
async def test_option_b_a_destination_inside_a_full_name_stays_masked(text: str, hits: list, gone: list) -> None:
    r = await scrub_for(text, VN, *hits)
    assert not any(g in r.text for g in gone), r.text
    assert "PLACE_KEPT" not in r.receipt["counts"]
    assert restore(r.text, r.placeholders) == text


async def test_option_b_a_destination_next_to_a_placeholder_stays_masked() -> None:
    # A stored person masked by pass 1 right before the destination: "Contact <PERSON_1> Hội An tomorrow".
    lan = KnownPerson("primary", display_name="Thanh Lan")
    finds = Finds(("NAME", "Hội An"))
    r = await PiiScrubber(finds, today=TODAY).scrub(
        "Contact Thanh Lan Hội An tomorrow.", KnownValues((lan,), destinations=VN)
    )
    assert finds.seen == ["Contact <PERSON_1> Hội An tomorrow."]
    assert "Hội An" not in r.text and "PLACE_KEPT" not in r.receipt["counts"]


async def test_option_b_everyday_words_next_to_a_destination_keep_it() -> None:
    # "từ" (from) is the family name "Từ" only when capitalised; English "To" / "Do" are never family names here;
    # a place after a comma or a dash is not touching a name.
    for text, hit in (
        ("Đi từ Hội An ra Đà Nẵng.", ("NAME", "Hội An")),
        ("To Sa Pa by night bus.", ("NAME", "Sa Pa")),
        ("Do Hoi An first, then Hue.", ("NAME", "Hoi An")),
        ("Khách Lê, Hội An là điểm đầu.", ("NAME", "Hội An")),
        ("Tour Hạ Long 2 ngày.", ("NAME", "Hạ Long")),
        ("Hà Nội - Sa Pa 3 đêm.", ("NAME", "Sa Pa")),
    ):
        r = await scrub_for(text, VN, hit)
        assert r.text == text and r.receipt["counts"] == {"PLACE_KEPT": 1}, text


async def test_option_b_decomposed_text_is_read_as_composed() -> None:
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Đi Hội An rồi Thanh Hóa.")
    r = await scrub_for(
        nfd, VN, ("NAME", unicodedata.normalize("NFD", "Hội An")), ("NAME", unicodedata.normalize("NFD", "Thanh Hóa"))
    )
    assert unicodedata.normalize("NFD", "Hội An") in r.text  # kept, as the composed spelling would be
    assert unicodedata.normalize("NFD", "Thanh Hóa") not in r.text  # name-like whatever the normal form
    assert r.receipt["counts"]["PLACE_KEPT"] == 1


async def test_option_b_place_plus_more_and_merges_stay_masked() -> None:
    hits = (("ADDRESS", "12 Hội An road"), ("ADDRESS", "Hội An"))
    assert (await scrub_for("Ship to 12 Hội An road.", VN, *hits)).text == "Ship to <ADDRESS_1>."
    r = await scrub_for("Phòng Hội An + Sa Pa.", VN, ("ADDRESS", "Hội An + Sa Pa"))
    assert r.text == "Phòng <ADDRESS_1>."
    # Overlapping NAME + ADDRESS on the same keepable words: both keepable, kept.
    r = await scrub_for("Đi Hội An.", VN, ("NAME", "Hội An"), ("ADDRESS", "Hội An"))
    assert r.text == "Đi Hội An."
    # Overlapping NAME + ADDRESS on name-like words: the f3-2 kind (ADDRESS).
    r = await scrub_for("Guest Mai Châu.", VN, ("NAME", "Mai Châu"), ("ADDRESS", "Mai Châu"))
    assert r.text == "Guest <ADDRESS_1>."
    # A keepable piece merged with a non-keepable one (a person's name overlapping it) is masked whole.
    r = await scrub_for("Hội Anh Tuấn", VN, ("ADDRESS", "Hội An"), ("NAME", "Anh Tuấn"))
    assert "Hội" not in r.text and "Tuấn" not in r.text
    # Only NAME / ADDRESS findings can be kept.
    r = await scrub_for("User Sapa logged in.", VN, ("USERNAME", "Sapa"))
    assert r.text == "User <USERNAME_1> logged in."


async def test_option_b_restore_round_trips_and_known_persons_stay_pass_1s() -> None:
    hg = KnownPerson("companion", display_name="Hội An")  # stored: pass 1 masks it, whatever its spelling
    finds = Finds(("NAME", "Sa Pa"))
    r = await PiiScrubber(finds, today=TODAY).scrub("Hội An đi Sa Pa.", KnownValues((hg,), destinations=VN))
    assert r.text == "<PERSON_1> đi Sa Pa." and finds.seen == ["<PERSON_1> đi Sa Pa."]
    assert restore(r.text, r.placeholders) == "Hội An đi Sa Pa."


@pytest.mark.parametrize(("text", "hits", "expected"), LIVE_PROBE)
async def test_option_b_live_probe_people_stay_masked(text: str, hits: list, expected: str) -> None:
    """The S249 probe replayed with the trip in Vietnam: every person stays masked; only Sa Pa / Hạ Long are clear."""
    r = await scrub_for(text, VN, *hits)
    places = {"Sa Pa", "Hạ Long"}
    for _, value in hits:
        assert (value in r.text) == (value in places), value
    assert restore(r.text, r.placeholders) == text


async def test_keepable_places_guard() -> None:
    from app.intake.stage1 import DESTINATIONS
    from app.privacy.names import name_like
    from app.privacy.places import KEEPABLE_PLACES, is_keepable_place

    assert set(KEEPABLE_PLACES) <= set(DESTINATIONS) - {"not_sure"}
    for country, phrases in KEEPABLE_PLACES.items():
        for phrase in phrases:
            assert not name_like(phrase), phrase  # the rule: no listed place reads as a person's name
            assert is_keepable_place(phrase, (country,)) and not is_keepable_place(phrase, ()), phrase
    for place in NAME_LIKE_PLACES:
        assert name_like(place) and not any(is_keepable_place(place, (c,)) for c in KEEPABLE_PLACES), place
    assert name_like("") and name_like("12 / 3")  # no word at all: fail closed
    import unicodedata

    for place in ("Thanh Hóa", "Ninh Bình", "Hà Giang", "Mai Châu"):  # decomposed text reads the same
        assert name_like(unicodedata.normalize("NFD", place)), place
    assert not name_like(unicodedata.normalize("NFD", "Hội An"))
    for dropped in ("Mandalay", "Paro", "Bali"):  # S250 review: given names too, never kept
        assert not any(is_keepable_place(dropped, (c,)) for c in KEEPABLE_PLACES), dropped
    for yes in ("hạ long", "SA PA", "Sa-Pa", " (Hội An), ", "Hội An"):
        assert is_keepable_place(yes, VN), yes
    for no in ("Hà Long", "Ha Long", "Sa Pa 2", "đi Sa Pa", "", "Bangkok"):
        assert not is_keepable_place(no, VN), no
