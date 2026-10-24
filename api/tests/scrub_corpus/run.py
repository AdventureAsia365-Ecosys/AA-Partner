"""Run the F3 corpus through the scrubber and count leaks (card CRM-10 checklist; design v3 §4.5 "0 leaks").

Two modes:

- **offline** (every CI run, ``tests/unit/test_scrub_corpus.py``): passes 1 and 2 with a detector that finds nothing;
  samples marked ``needs_detector`` are skipped. Free.
- **guardrails** (``python -m tests.scrub_corpus.run --guardrails``; workflow "Scrub corpus", nightly and on every
  push to main that changes the scrubber or the corpus): all three passes against the real guardrail named by
  ``SCRUB_GUARDRAIL_ID`` / ``SCRUB_GUARDRAIL_VERSION`` (acc2, about 240 text units a run, about $0.024).

A **leak** = a ``pii`` string still readable in the scrubbed text: folded (no diacritics, any case) as a whole-word
sequence, or, for values with 6+ digits, the same digits anywhere in the text once placeholders are removed. Leaks
must be 0 (a one-word value inside a known place name is the place, not a leak). An **over-scrub** = a ``keep``
string that did not survive; reported, and the run fails above
:data:`MAX_OVERSCRUB_RATE` (a given name that is also a place, "Hà" in "Hà Giang", is a known, accepted cost).
A **typed place** = a ``keep`` string that is back once only the ``<PLACE_n>`` placeholders are restored (option C,
S249: the detector's ADDRESS finding on a bare destination is still masked, but typed, so the model knows a place
stood there). It is neither an over-scrub nor a pass; it is reported on its own (``typed_place``, rate over the keep
strings). Leaks are counted exactly as before: a ``<PLACE_n>`` hides its value like every placeholder.
The report has ids and counts only, never a sample's values (the corpus is synthetic, but the habit stays).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.privacy import patterns
from app.privacy.places import place_spans
from app.privacy.scrubber import PLACE, Detection, KnownPerson, KnownValues, PiiScrubber, restore
from app.privacy.text import digits, fold

CORPUS = Path(__file__).with_name("corpus.jsonl")
MIN_SAMPLES = 200
MAX_OVERSCRUB_RATE = 0.05
TODAY = dt.date(2026, 10, 24)  # fixed, so "two years in the past" never moves under the corpus


class NoDetector:
    """Offline pass 3: finds nothing (the offline run skips samples that need the detector)."""

    name = "none"

    async def detect(self, text: str) -> Sequence[Detection]:
        return []


@dataclass
class Report:
    samples: int = 0
    skipped: int = 0
    leaks: list[str] = field(default_factory=list)  # "<id>:<pii index>"
    overscrubbed: list[str] = field(default_factory=list)
    typed_place: list[str] = field(default_factory=list)  # keep strings masked as <PLACE_n> (option C)
    keep_total: int = 0

    @property
    def overscrub_rate(self) -> float:
        return len(self.overscrubbed) / self.keep_total if self.keep_total else 0.0

    @property
    def typed_place_rate(self) -> float:
        return len(self.typed_place) / self.keep_total if self.keep_total else 0.0

    def ok(self) -> bool:
        return not self.leaks and self.overscrub_rate <= MAX_OVERSCRUB_RATE

    def summary(self) -> dict:
        return {
            "samples": self.samples,
            "skipped": self.skipped,
            "leaks": len(self.leaks),
            "leak_ids": self.leaks[:50],
            "overscrubbed": len(self.overscrubbed),
            "overscrub_rate": round(self.overscrub_rate, 4),
            "overscrub_ids": self.overscrubbed[:50],
            "keep_total": self.keep_total,
            "typed_place": len(self.typed_place),
            "typed_place_rate": round(self.typed_place_rate, 4),
            "typed_place_ids": self.typed_place[:50],
        }


def load(path: Path = CORPUS) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def known_of(sample: dict) -> KnownValues:
    persons = []
    for p in sample.get("known", []):
        persons.append(
            KnownPerson(
                role=p.get("role", "traveller"),
                display_name=p.get("display_name"),
                given_name=p.get("given_name"),
                family_name=p.get("family_name"),
                emails=tuple(p.get("emails", ())),
                phones=tuple(p.get("phones", ())),
            )
        )
    return KnownValues(tuple(persons))


def _readable(value: str, scrubbed: str) -> bool:
    folded_out = fold(scrubbed)
    tokens = [re.escape(t) for t in fold(value).split()]
    if len(tokens) == 1:
        # A one-word value inside a place name ("Bình" in "Ninh Bình") is the place (app.privacy.places).
        for start, end in place_spans(folded_out):
            folded_out = folded_out[:start] + " " * (end - start) + folded_out[end:]
    if tokens and re.search(r"(?<!\w)" + r"\s+".join(tokens) + r"(?!\w)", folded_out):
        return True
    d = digits(value)
    if len(d) >= 6:
        return d in digits(patterns.PLACEHOLDER.sub(" ", scrubbed))
    return False


def places_only(placeholders: dict[str, str] | None) -> dict[str, str]:
    """The ``<PLACE_n>`` entries of a placeholder map (option C)."""
    return {ph: v for ph, v in (placeholders or {}).items() if ph.startswith(f"<{PLACE}_")}


async def evaluate(scrubber: PiiScrubber, samples: list[dict], *, offline: bool) -> Report:
    report = Report()
    for s in samples:
        if offline and s.get("needs_detector"):
            report.skipped += 1
            continue
        report.samples += 1
        result = await scrubber.scrub(s["text"], known_of(s))
        report.leaks += [f"{s['id']}:{i}" for i, v in enumerate(s["pii"]) if _readable(v, result.text)]
        places_back = restore(result.text, places_only(result.placeholders))
        for i, keep in enumerate(s.get("keep", [])):
            report.keep_total += 1
            if keep in result.text:
                continue
            (report.typed_place if keep in places_back else report.overscrubbed).append(f"{s['id']}:{i}")
    return report


def main(argv: list[str]) -> int:
    samples = load()
    if len(samples) < MIN_SAMPLES:
        print(json.dumps({"error": f"corpus has {len(samples)} samples, needs {MIN_SAMPLES}"}))
        return 2
    if "--guardrails" in argv:
        from app.privacy.guardrails import BedrockGuardrailsDetector

        timeout_s = float(os.environ.get("SCRUB_DETECTOR_TIMEOUT_MS", "5000")) / 1000
        detector = BedrockGuardrailsDetector(
            os.environ["SCRUB_GUARDRAIL_ID"],
            os.environ["SCRUB_GUARDRAIL_VERSION"],
            region=os.environ.get("SCRUB_GUARDRAIL_REGION", "us-west-1"),
            timeout_s=timeout_s,
        )
        report = asyncio.run(evaluate(PiiScrubber(detector, timeout_s=timeout_s, today=TODAY), samples, offline=False))
    else:
        report = asyncio.run(evaluate(PiiScrubber(NoDetector(), today=TODAY), samples, offline=True))
    print(json.dumps({"mode": "guardrails" if "--guardrails" in argv else "offline", **report.summary()}))
    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
