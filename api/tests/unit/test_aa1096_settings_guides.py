"""AA-1096 L4 — settings-guide lint: every CRM setting explains itself to an operator on CIS Platform > Settings.

The CIS page (AA-CIS-App CIS 416, ``frontend/lib/settingsRegistry.ts`` ``guideFor``) shows a short "what it controls"
line and an Explain panel from the ``guide`` of each definition; the CRM tab gets it from ``crm.ops.v1`` ``/settings``
(``app.platform_settings.service.definition_out``). The CRM's guides live in ``app/platform_settings/guides.json``
(crm.setting_definition has no guide column). The rules are the CIS lint's (AA-CIS-App
``tests/unit/test_aa1096_settings_guides.py``):

    guide = {"en": {"what", "raise", "lower", "when", "example"}, "vi": {...the same five parts}}

  1. every key has a complete guide (both languages, five parts of 10-800 characters, Vietnamese not a copy);
  2. every guide names a key of the compiled defaults (no orphan);
  3. no issue id (AA-123) and no internal stage code (T7, A1, S279 ...) in a label or a guide;
  4. the English example names the default value and never says "Today ..." (the value in force can change);
  5. the ops answer carries the guide (definition_out) and the crm.ops.v1 key allowlist lets it through.
"""

from __future__ import annotations

import json
import re

import pytest

from app import platform_settings as ps
from app.ops import serializer
from app.platform_settings.service import definition_out
from app.routers.ops_settings import SettingDefinitionOut

LANGS = ("en", "vi")
PARTS = ("what", "raise", "lower", "when", "example")
ISSUE_ID = re.compile(r"\bAA-\d+")
STAGE_CODE = re.compile(r"\b(?!S3\b)[ATSF]\d{1,3}\b")  # Amazon S3 is a product name, not a stage
NUMERIC = ("int", "numeric", "duration_s", "usd", "pct")
TODAY = re.compile(r"^\s*(Today|Hiện là|Hiện đang)\b")


def _definitions() -> list[dict]:
    return ps.load_definitions()


def guide_problems(guide: object) -> list[str]:
    """Why ``guide`` is not complete ([] = complete). Pure."""
    if not isinstance(guide, dict):
        return ["no guide"]
    out = []
    for lang in LANGS:
        part_map = guide.get(lang)
        if not isinstance(part_map, dict):
            out.append(f"no {lang} guide")
            continue
        for part in PARTS:
            text = part_map.get(part)
            if not isinstance(text, str) or not 10 <= len(text.strip()) <= 800:
                out.append(f"{lang}.{part} missing or not 10-800 characters")
    if not out:
        same = [p for p in PARTS if guide["en"][p].strip() == guide["vi"][p].strip()]
        out.extend(f"vi.{p} is a copy of en.{p}" for p in same)
    return out


def default_words(d: dict) -> list[str]:
    """The ways the English example may name the default (any one must appear). [] = not checked."""
    v, t = d["default_value"], d["value_type"]
    if t == "bool":
        return ["on"] if v else ["off"]
    if t == "enum":
        return [str(v)]
    if t in NUMERIC:
        n = float(v)
        plain = str(int(n)) if n.is_integer() else repr(n)
        return sorted({plain, f"{n:,.0f}" if n.is_integer() else plain, f"{n:.2f}"})
    return []


def test_every_crm_setting_has_a_complete_guide_in_both_languages() -> None:
    missing = {d["key"]: guide_problems(ps.guide(d["key"])) for d in _definitions()}
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, f"CRM settings without a complete EN + VI guide (guides.json): {json.dumps(missing, indent=1)}"


def test_every_guide_names_a_crm_setting() -> None:
    keys = {d["key"] for d in _definitions()}
    orphans = sorted(set(ps.GUIDES) - keys)
    assert not orphans, f"guides.json names keys that are no CRM setting: {orphans}"


def test_labels_and_guides_carry_no_issue_ids_or_stage_codes() -> None:
    for d in _definitions():
        texts = [d["label"]] + [t for lang in (ps.guide(d["key"]) or {}).values() for t in lang.values()]
        for text in texts:
            assert not ISSUE_ID.search(text), f"{d['key']}: an issue id in operator text: {text!r}"
            assert not STAGE_CODE.search(text), f"{d['key']}: an internal stage code in operator text: {text!r}"


def test_the_example_names_the_default_and_never_says_today() -> None:
    for d in _definitions():
        guide = ps.guide(d["key"]) or {}
        for lang in LANGS:
            example = (guide.get(lang) or {}).get("example") or ""
            assert not TODAY.search(example), f"{d['key']}: the {lang} example says 'today': {example!r}"
        words = default_words(d)
        example = (guide.get("en") or {}).get("example")
        if words and example:
            assert any(w in example for w in words), f"{d['key']}: the example does not name the default {words}"


def test_the_ops_answer_carries_the_guide_through_the_key_allowlist() -> None:
    key = ps.RATE_PUBLIC
    out = definition_out(ps.definition(key))
    assert out["guide"] == ps.guide(key)
    assert set(out["guide"]) == set(LANGS) and all(set(out["guide"][lang]) == set(PARTS) for lang in LANGS)
    assert serializer.bad_keys(out) == set()
    assert serializer.drop_keys(out) == out
    # the route's response model keeps the part names the page reads (``raise`` is a Python keyword: an alias)
    served = SettingDefinitionOut.model_validate(out).model_dump(by_alias=True)
    assert served["guide"] == out["guide"]


@pytest.mark.parametrize(
    "guide,expected",
    [
        (None, ["no guide"]),
        ({"en": dict.fromkeys(PARTS, "long enough text")}, ["no vi guide"]),
        ({"en": dict.fromkeys(PARTS, "long enough text"), "vi": dict.fromkeys(PARTS, "đủ dài để đọc")}, []),
        (
            {
                "en": dict.fromkeys(PARTS, "long enough text"),
                "vi": {**dict.fromkeys(PARTS, "đủ dài để đọc"), "when": " "},
            },
            ["vi.when missing or not 10-800 characters"],
        ),
        (
            {
                "en": dict.fromkeys(PARTS, "long enough text"),
                "vi": {**dict.fromkeys(PARTS, "đủ dài để đọc"), "example": "long enough text"},
            },
            ["vi.example is a copy of en.example"],
        ),
    ],
)
def test_guide_problems(guide: object, expected: list[str]) -> None:
    assert guide_problems(guide) == expected


@pytest.mark.parametrize(
    "d,words",
    [
        ({"value_type": "bool", "default_value": False}, ["off"]),
        ({"value_type": "enum", "default_value": "soft"}, ["soft"]),
        ({"value_type": "int", "default_value": 2000}, ["2,000", "2000", "2000.00"]),
        ({"value_type": "usd", "default_value": 0.25}, ["0.25"]),
        ({"value_type": "json", "default_value": {"a": 1}}, []),
    ],
)
def test_default_words(d: dict, words: list[str]) -> None:
    assert default_words(d) == sorted(set(words))
