"""The F3 corpus gate, offline half (card CRM-10 checklist: ">= 200 samples, nightly + on scrubber change").

Every PR runs passes 1 and 2 over the corpus: 0 leaks, over-scrub within the limit; and, with a detector that tags
every destination ADDRESS as the live guardrail does, option C's typed places (``<PLACE_n>``) are not over-scrub.
The Guardrails half (all three passes) is the "Scrub corpus" workflow (nightly and on every push to main that changes
app/privacy or the corpus).
"""

from __future__ import annotations

import asyncio
import re

from app.privacy.places import TYPED_PLACE_RE
from app.privacy.scrubber import Detection, ScrubResult
from app.privacy.text import fold_with_map, original_span
from tests.scrub_corpus import generate, run


def test_corpus_size_languages_and_e_cases() -> None:
    samples = run.load()
    assert len(samples) >= run.MIN_SAMPLES
    langs = {s["lang"] for s in samples}
    assert langs == {"vi", "en"}
    assert sum(s["lang"] == "vi" for s in samples) >= 100 and sum(s["lang"] == "en" for s in samples) >= 100
    assert len({s["id"] for s in samples}) == len(samples)
    assert any(s["needs_detector"] for s in samples)  # names nobody stored: only Guardrails can find them
    assert all("example.invalid" in e for s in samples for p in s["known"] for e in p.get("emails", []))


def test_corpus_is_the_generator_output() -> None:
    assert generate.main(["--check"]) == 0


def test_offline_corpus_has_no_leaks() -> None:
    report = asyncio.run(run.evaluate(run.PiiScrubber(run.NoDetector(), today=run.TODAY), run.load(), offline=True))
    assert report.samples >= run.MIN_SAMPLES and report.leaks == [], report.summary()
    assert report.overscrub_rate <= run.MAX_OVERSCRUB_RATE, report.summary()
    assert report.ok()


def test_a_scrubber_that_does_nothing_leaks_everything() -> None:
    """The leak check itself works: the identity scrubber leaks every sample."""

    class Identity:
        version = "none"

        async def scrub(self, text, known=None):  # noqa: ANN001, ANN202
            return ScrubResult(text, {})

    report = asyncio.run(run.evaluate(Identity(), run.load(), offline=True))  # type: ignore[arg-type]
    assert len(report.leaks) >= report.samples and not report.ok()


def test_cli_offline_mode(capsys) -> None:  # noqa: ANN001
    assert run.main([]) == 0
    assert '"leaks": 0' in capsys.readouterr().out


class PlaceTagger:
    """Replays what the live guardrail did to the corpus (run 40076803898, S249): every bare destination comes back as
    an ADDRESS finding. Offline and free; the real check stays the "Scrub corpus" workflow."""

    name = "place-tagger"

    async def detect(self, text: str) -> list[Detection]:
        folded, index = fold_with_map(text)
        out = []
        for m in re.finditer(r"(?<!\w)" + TYPED_PLACE_RE.pattern + r"(?!\w)", folded):
            start, end = original_span(index, m.start(), m.end())
            out.append(Detection("ADDRESS", text[start:end]))
        return out


def test_option_c_destinations_tagged_as_address_are_typed_places_not_overscrub() -> None:
    scrubber = run.PiiScrubber(PlaceTagger(), today=run.TODAY)
    report = asyncio.run(run.evaluate(scrubber, run.load(), offline=True))
    summary = report.summary()
    assert report.leaks == [], summary
    # Before option C every one of these was an over-scrub (<ADDRESS_n>), as in the live run (28.5 %).
    assert len(report.typed_place) >= 80 and summary["typed_place_rate"] > run.MAX_OVERSCRUB_RATE, summary
    assert report.overscrub_rate <= run.MAX_OVERSCRUB_RATE and report.ok(), summary


def test_typed_place_is_counted_apart_from_overscrub() -> None:
    class Fixed:
        version = "fixed"

        def __init__(self, text: str, placeholders: dict[str, str]) -> None:
            self.result = ScrubResult(text, {}, placeholders)

        async def scrub(self, text, known=None):  # noqa: ANN001, ANN202
            return self.result

    sample = {"id": "t-1", "text": "x", "pii": [], "known": [], "keep": ["Hà Giang", "Ha Long Bay", "2 adults"]}
    out = "<PLACE_1>, <ADDRESS_1> Bay, 2 adults"
    fixed = Fixed(out, {"<PLACE_1>": "Hà Giang", "<ADDRESS_1>": "Ha Long"})
    report = asyncio.run(run.evaluate(fixed, [sample], offline=True))  # type: ignore[arg-type]
    assert report.typed_place == ["t-1:0"] and report.overscrubbed == ["t-1:1"] and report.keep_total == 3
    assert report.summary()["typed_place"] == 1 and report.summary()["typed_place_rate"] == round(1 / 3, 4)
