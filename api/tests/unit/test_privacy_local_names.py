"""Pass 3's local name check (S286, scrubber f3-4): person names found without the detector.

The nightly corpus run of 20/11/2026 went red with the same guardrail version: it stopped tagging people named like
places in lists and after person words, and one Vietnamese full name. The local check masks those shapes with no
detector finding at all. Every name below is invented. Split from tests/unit/test_privacy_scrubber.py (800-line limit).
"""

from __future__ import annotations

import asyncio
import time
import unicodedata

import pytest

from app.privacy import scrubber as scrubber_module
from app.privacy.local_names import person_names
from app.privacy.scrubber import Detection, KnownPerson, PiiScrubber, ScrubError, ScrubTimeout, restore
from tests.unit.test_privacy_scrubber import TODAY, VN, Finds, run, scrub_for

pytestmark = pytest.mark.asyncio

LOCAL_PERSON_SHAPES = [
    ("Khách Đoàn Gia Khiêm giới thiệu tour này.", ["Đoàn Gia Khiêm"]),
    ("Người đặt: VƯƠNG THỊ NGỌC TRÂM, đã cọc.", ["VƯƠNG THỊ NGỌC TRÂM"]),
    ("Guests: Hòa Bình and Kim Liên, both vegetarian.", ["Hòa Bình", "Kim Liên"]),
    ("Danh sách khách: Long An, Hà Nam, Thái Bình.", ["Long An", "Hà Nam", "Thái Bình"]),
    ("Phòng 3: Hải Dương & Mai Châu. Phòng 4: Lâm Đồng.", ["Hải Dương", "Mai Châu", "Lâm Đồng"]),
    ("Room 2: Cao Bằng + Thanh Hóa, twin beds.", ["Cao Bằng", "Thanh Hóa"]),
    ("Chị Kim Liên đi cùng chồng là Phú Thọ.", ["Kim Liên", "Phú Thọ"]),
    ("Her husband is Ninh Bình, arriving late.", ["Ninh Bình"]),
    ("Passenger Hà Giang asked about the bus.", ["Hà Giang"]),
    ("Passengers: Mrs Hà Nam, Mr Hòa Bình.", ["Hà Nam", "Hòa Bình"]),
    ("Mr.Hội An called; (Ms) Nha Trang will pay.", ["Hội An", "Nha Trang"]),
    ("Khách Lê Hội An đổi ngày.", ["Lê Hội An"]),
    # S286 review: a list is walked once; a cue inside a list already walked still cues the item after it.
    ("Travellers: Mai Châu, Guest Hội An and Mrs Ninh Bình.", ["Mai Châu", "Hội An", "Ninh Bình"]),
    ("Khách Long An, khách Hội An, Mr Mr Sa Pa.", ["Long An", "Hội An", "Sa Pa"]),
]


@pytest.mark.parametrize(("text", "people"), LOCAL_PERSON_SHAPES)
async def test_local_names_mask_people_the_detector_did_not_tag(text: str, people: list[str]) -> None:
    for destinations in ((), VN):  # option B never keeps a person found this way
        r = await scrub_for(text, destinations)
        for person in people:
            assert person not in r.text, (person, r.text)
            assert person in r.placeholders.values(), person
        assert all(ph.startswith("<PERSON_") for ph in r.placeholders), r.placeholders
        assert r.receipt["passes"]["detector"] == r.receipt["counts"]["PERSON"] > 0
        assert restore(r.text, r.placeholders) == text


@pytest.mark.parametrize(
    "text",
    [
        "Từ Hà Nội đi Sa Pa, rồi về Hồ Hoàn Kiếm và Hà Nội Old Quarter.",
        "Đi Thái Lan hay Hàn Quốc? Châu Á mùa này đẹp; Hồ Tây buổi tối.",
        "Khách sạn Hà Nội gần Hồ Gươm; khách hàng Đà Nẵng hỏi giá.",
        "Don't miss Hội An; Hoi An Ancient Town at night; Ha Long Bay cruise.",
        "To Hoi An by train, Le Mans style rally, Pham noodles, Do Son beach.",
        "Hồ Chí Minh City, then Hà Giang Loop, then Mai Châu.",
        "Tour Lâm Đồng 3 ngày; Ninh Bình + Hải Dương by car.",
        "Our top ten Hội An cafés; the son et lumière show.",
    ],
)
async def test_local_names_leave_places_and_everyday_words(text: str) -> None:
    r = await scrub_for(text, VN)
    assert r.text == text and r.placeholders == {}, r.text


async def test_local_names_only_fill_what_the_detector_missed() -> None:
    # The detector covers Hà Giang (typed place, as before): unchanged. It misses Kim Liên: the local check masks it.
    r = await scrub_for("Danh sách khách: Hà Giang, Kim Liên.", (), ("ADDRESS", "Hà Giang"))
    assert r.text == "Danh sách khách: <PLACE_1>, <PERSON_1>."
    # A detector finding wider than the local one ("Mrs Hà Nam") covers it: one placeholder, the detector's.
    r = await scrub_for("Passengers: Mrs Hà Nam.", (), ("NAME", "Mrs Hà Nam"))
    assert r.text == "Passengers: <PERSON_1>." and r.placeholders == {"<PERSON_1>": "Mrs Hà Nam"}
    # Stored people are pass 1's: the local check only ever sees their placeholder.
    known = KnownPerson("traveller", display_name="Đoàn Gia Khiêm")
    r = await scrub_for("Khách Đoàn Gia Khiêm và Long An.", (), persons=(known,))
    assert r.text == "Khách <PERSON_1> và <PERSON_2>."
    assert r.receipt["passes"] == {"known": 1, "regex": 0, "detector": 1}


async def test_local_names_read_decomposed_text_and_keep_trip_destinations() -> None:
    text = unicodedata.normalize("NFD", "Khách Đoàn Gia Khiêm đi Hội An, guide Hà Giang.")
    r = await scrub_for(text, VN, ("NAME", unicodedata.normalize("NFD", "Hội An")))
    assert "Đoàn" not in unicodedata.normalize("NFC", r.text) and "Giang" not in r.text
    assert unicodedata.normalize("NFC", r.text) == "Khách <PERSON_1> đi Hội An, guide <PERSON_2>."
    assert r.receipt["counts"]["PLACE_KEPT"] == 1  # option B still keeps the trip's own destination


async def test_a_bug_in_the_local_name_check_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(text: str) -> list:
        raise RuntimeError("Đoàn Gia Khiêm")

    monkeypatch.setattr(scrubber_module, "local_person_findings", broken)
    with pytest.raises(ScrubError) as caught:
        await run("Khách Đoàn Gia Khiêm")
    assert "Khiêm" not in str(caught.value)  # recorded by class only
    # A local value that cannot be placed in the text refuses the scrub, like a detector finding.
    monkeypatch.setattr(scrubber_module, "local_person_findings", lambda text: [Detection("NAME", "Lâm Quốc Vinh")])
    with pytest.raises(ScrubError, match="not located"):
        await run("Khách Đoàn Gia Khiêm")


async def test_a_slow_local_name_check_fails_closed_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # S286 review: the local check ran on the event loop, outside the deadline. Now a worker thread under the deadline.
    def slow(text: str) -> list:
        time.sleep(0.5)
        return []

    monkeypatch.setattr(scrubber_module, "local_person_findings", slow)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.perf_counter()
    try:
        with pytest.raises(ScrubTimeout):
            await PiiScrubber(Finds(), timeout_s=0.2, today=TODAY).scrub("Khách Đoàn Gia Khiêm")
    finally:
        task.cancel()
    assert time.perf_counter() - started < 0.45  # the deadline, not the slow check, ends the scrub
    assert ticks >= 5  # the event loop kept running meanwhile


@pytest.mark.parametrize(
    ("text", "people"),
    [
        # The cue's "là" / "is" can end inside a word: the rest of that word opens the item.
        ("Khách laHà Giang đến.", ["Hà Giang"]),
        ("Khách laHuế đến.", ["Huế"]),
        ("Khách laMrs Huế", ["Huế"]),
        ("Mr isMr Sa Pa, Mr isMr.", ["Sa Pa"]),
        ("Mr isMr Mr Mr Mr Mr Mr Mr Mr Mr Mr Sa Pa.", ["Sa Pa"]),
        # A run longer than any known place is not one (only rule (a) reads its full names).
        ("Khách laHuế Bình Lâm Kim Đồng Hải Mai Châu Vân Na.", ["Lâm Kim Đồng Hải", "Châu Vân Na"]),
    ],
)
async def test_local_names_item_opening_inside_a_word(text: str, people: list[str]) -> None:
    assert person_names(text) == people


async def test_local_names_values_come_once_in_order() -> None:
    text = "Khách Đoàn Gia Khiêm, Long An và <PERSON_1>; Đoàn Gia Khiêm gọi lại. Room 5: Hà Nam."
    assert person_names(text) == ["Đoàn Gia Khiêm", "Long An", "Hà Nam"]
    assert person_names("") == [] and person_names("Khách: ") == [] and person_names("Mrs ") == []


@pytest.mark.parametrize(
    ("text", "hits", "expected"),
    [
        # A person named like a keepable destination right after a room line (a cue option B's guard does not know):
        # the guardrail's NAME alone would be kept; the local finding keeps it masked.
        ("Room 1: Hội An.", [("NAME", "Hội An")], "Room 1: <PERSON_1>."),
        ("Room 1: Hội An + Thái Bình.", [("NAME", "Hội An")], "Room 1: <PERSON_1> + <PERSON_2>."),
        ("Room 1: Hội An.", [], "Room 1: <PERSON_1>."),
        # The trip's own destination with no person cue: still clear.
        ("Đi Hội An rồi Sa Pa.", [("NAME", "Hội An"), ("ADDRESS", "Sa Pa")], "Đi Hội An rồi Sa Pa."),
    ],
)
async def test_local_names_are_never_kept_as_a_trip_destination(text: str, hits: list, expected: str) -> None:
    r = await scrub_for(text, VN, *hits)
    assert r.text == expected
    assert restore(r.text, r.placeholders) == text
