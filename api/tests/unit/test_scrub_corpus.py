"""The F3 corpus gate, offline half (card CRM-10 checklist: ">= 200 samples, nightly + on scrubber change").

Every PR runs passes 1 and 2 over the corpus: 0 leaks, over-scrub within the limit; and, with a detector that tags
every destination ADDRESS as the live guardrail does, option C's typed places (``<PLACE_n>``) are not over-scrub.
The Guardrails half (all three passes) is the "Scrub corpus" workflow (nightly and on every push to main that changes
app/privacy or the corpus).
"""

from __future__ import annotations

import asyncio
import re

from app.privacy.names import name_like
from app.privacy.places import TYPED_PLACE_RE, is_place
from app.privacy.scrubber import Detection, ScrubResult
from app.privacy.text import fold, fold_with_map, original_span
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
    # Option C alone: without the trip's destinations (option B) nothing is kept, every bare destination is typed.
    scrubber = run.PiiScrubber(PlaceTagger(), today=run.TODAY)
    samples = [{**s, "destinations": []} for s in run.load()]
    report = asyncio.run(run.evaluate(scrubber, samples, offline=True))
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


# ── Option B (AA-938, S250, scrubber f3-3): the trip's own non-name-like destinations stay in clear ───────────────

# The 33 over-scrubs of the live run 40081230173 (S250 start, scrubber f3-2; ids only): 25 destinations the guardrail
# tagged NAME, 6 "2030" passport-expiry years and 2 "Northern Loop" tour names.
LIVE_OVERSCRUB_IDS = (
    "vi-002:1 vi-035:1 vi-054:0 vi-057:1 vi-087:0 en-112:1 en-116:0 en-122:0 en-138:0 en-144:0 en-145:0 en-147:1 "
    "en-149:0 en-152:0 en-155:0 en-156:1 en-158:1 en-167:1 en-178:0 en-180:1 en-182:0 en-185:0 en-189:1 en-191:1 "
    "en-202:1 en-204:0 en-210:0 en-211:1 en-213:1 en-215:0 en-224:0 en-225:0 en-226:0"
).split()


class NameTagger(PlaceTagger):
    """Every destination comes back as a NAME finding: the worst case for option B. Offline and free."""

    name = "name-tagger"

    async def detect(self, text: str) -> list[Detection]:
        return [Detection("NAME", d.match) for d in await super().detect(text)]


class LiveReplay(PlaceTagger):
    """One sample as the live run saw it: the keep strings it over-scrubbed come back as the finding that masked
    them (a destination as NAME, "2030" as a card expiry, a tour name as an address), every other destination as
    ADDRESS (typed places in the live run)."""

    name = "live-replay"

    def __init__(self, sample: dict) -> None:
        ids = set(LIVE_OVERSCRUB_IDS)
        self.hits = [k for i, k in enumerate(sample["keep"]) if f"{sample['id']}:{i}" in ids]

    async def detect(self, text: str) -> list[Detection]:
        out = []
        for d in await super().detect(text):
            out.append(Detection("NAME" if d.match in self.hits else "ADDRESS", d.match))
        for k in self.hits:
            if k == "2030":
                out.append(Detection("CREDIT_DEBIT_CARD_EXPIRY", k))
            elif not TYPED_PLACE_RE.fullmatch(fold(k)):
                out.append(Detection("ADDRESS", k))
        return out


def _replay(tagger: PlaceTagger, samples: list[dict] | None = None) -> run.Report:
    """Offline half: samples that need the real detector (names that are not places) are skipped, as in run.py."""
    return asyncio.run(run.evaluate(run.PiiScrubber(tagger, today=run.TODAY), samples or run.load(), offline=True))


def _live_replay(samples: list[dict]) -> run.Report:
    total = run.Report()
    for s in samples:
        one = asyncio.run(run.evaluate(run.PiiScrubber(LiveReplay(s), today=run.TODAY), [s], offline=True))
        total.samples += one.samples
        total.skipped += one.skipped
        total.leaks += one.leaks
        total.overscrubbed += one.overscrubbed
        total.typed_place += one.typed_place
        total.keep_total += one.keep_total
        total.kept_place += one.kept_place
    return total


def _keep(samples: list[dict], ref: str) -> str:
    sample_id, i = ref.rsplit(":", 1)
    return next(s for s in samples if s["id"] == sample_id)["keep"][int(i)]


def test_option_b_every_destination_tagged_name_leaks_nothing_and_keeps_only_non_name_like_places() -> None:
    samples = run.load()
    report = _replay(NameTagger(), samples)
    summary = report.summary()
    assert report.leaks == [] and report.kept_place >= 60, summary
    for ref in report.overscrubbed:  # what is still masked is a name-like place (Ninh Bình, Huế, Mai Châu ...)
        assert name_like(_keep(samples, ref)), ref


def test_option_b_the_live_run_replayed_without_destinations_is_the_live_result() -> None:
    """The replay is faithful: without the trip's destinations (f3-2's behaviour) it gives the live run's 33."""
    samples = [{**s, "destinations": []} for s in run.load()]
    report = _live_replay(samples)
    assert report.leaks == [] and report.kept_place == 0, report.summary()
    assert sorted(report.overscrubbed) == sorted(LIVE_OVERSCRUB_IDS)


def test_option_b_the_live_run_replayed_fits_the_overscrub_budget() -> None:
    samples = run.load()
    report = _live_replay(samples)
    summary = report.summary()
    assert report.leaks == [] and report.kept_place >= 20, summary
    # Left: the name-like destinations (Ninh Binh x3, Mai Châu, Ha Long Bay), the "2030" years, the tour names.
    left = sorted({_keep(samples, ref) for ref in report.overscrubbed})
    assert left == ["2030", "Ha Long Bay", "Mai Châu", "Ninh Binh", "Northern Loop"], left
    assert len(report.overscrubbed) == 13 and report.overscrub_rate <= run.MAX_OVERSCRUB_RATE and report.ok(), summary


def test_option_b_name_like_strangers_and_places_outside_the_trip_stay_masked() -> None:
    samples = run.load()
    cue_texts = {text for _, text, _, _ in generate.PERSON_CUE_STRANGERS}
    strangers = [s for s in samples if s["needs_detector"] and not s["known"] and s["text"] not in cue_texts]
    outside = [s for s in samples if s.get("outside")]
    assert len(strangers) >= 9 and len(outside) >= 3
    assert all(name_like(p) for s in strangers for p in s["pii"] if " " in p)

    async def scrubbed(s: dict) -> str:
        return (await run.PiiScrubber(NameTagger(), today=run.TODAY).scrub(s["text"], run.known_of(s))).text

    for s in outside:
        text = asyncio.run(scrubbed(s))
        assert all(k in text for k in s["keep"]), s["id"]  # the trip's own destination: clear
        assert not any(p in text for p in s["outside"]), s["id"]  # another country's: masked as in f3-2
    for s in strangers:  # every person named like a place, tagged NAME (or ADDRESS) on a Vietnam trip: masked
        for tagger in (NameTagger(), PlaceTagger()):
            text = asyncio.run(run.PiiScrubber(tagger, today=run.TODAY).scrub(s["text"], run.known_of(s))).text
            assert s["destinations"] == ["vietnam"]
            assert not any(run._readable(p, text) for p in s["pii"] if is_place(p)), s["id"]


def test_option_b_people_named_like_a_keepable_destination_stay_masked() -> None:
    """S250 review (live on acc2): a person named exactly like a keepable destination, after a person cue ("Mr.Hội An",
    "My wife Hội An", "Name: Nha Trang") or next to a family name ("Khách Lê Hội An"), tagged NAME on a Vietnam trip."""
    samples = [s for s in run.load() if s["text"] in {text for _, text, _, _ in generate.PERSON_CUE_STRANGERS}]
    assert len(samples) == len(generate.PERSON_CUE_STRANGERS)

    class PiiTagger:
        name = "pii-tagger"

        def __init__(self, sample: dict) -> None:
            self.pii = sample["pii"]

        async def detect(self, text: str) -> list[Detection]:
            return [Detection("NAME", p) for p in self.pii]

    for s in samples:
        assert s["destinations"] == ["vietnam"] and not any(name_like(p) for p in s["pii"]), s["id"]  # keepable shapes
        r = asyncio.run(run.PiiScrubber(PiiTagger(s), today=run.TODAY).scrub(s["text"], run.known_of(s)))
        assert not any(run._readable(p, r.text) for p in s["pii"]), (s["id"], r.text)
        assert all(k in r.text for k in s["keep"]) and "PLACE_KEPT" not in r.receipt["counts"], s["id"]
