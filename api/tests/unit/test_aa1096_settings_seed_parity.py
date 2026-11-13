"""AA-1096 lane L4 (crm-064) — the compiled CRM settings defaults equal the migration seed (S279 audit 5.4 check 4).

``app/platform_settings/compiled_defaults.json`` is what the settings reader serves when the database cannot be read,
so it must be the seed of ``crm.setting_definition`` exactly: ``scripts/gen_settings_defaults.py --check`` fails when
they differ (fix: run the script and commit the file). The seed rows also obey the table's CHECKs here, so a bad row
fails in unit CI before the migration replay; and each definition's bounds stay inside the database CHECK of the
agency column it governs. No DB."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

API = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gen_settings_defaults", API / "scripts" / "gen_settings_defaults.py")
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

VALUE_TYPES = {"int", "numeric", "bool", "enum", "duration_s", "usd", "pct", "list", "json"}
NUMERIC = {"int", "numeric", "duration_s", "usd", "pct"}
KEY_RE = re.compile(r"^crm(\.[a-z0-9_]+){2,}$")
# The crm.tenant_settings CHECK bounds of the agency columns a platform default or range governs (crm-005, -008, -010,
# -046, -048, -062): a definition may never allow a value the column refuses.
COLUMN_BOUNDS = {
    "crm.ai.budget_max_usd": (0, 10000),
    "crm.ai.budget_default_usd": (0, 10000),
    "crm.payments.refund_owner_threshold_default": (0, 10000000),
    "crm.agency_defaults.discount_owner_limit_pct": (0, 100),
    "crm.agency_defaults.markup_max_pct": (0, 300),
    "crm.agency_defaults.price_checks_per_minute": (10, 600),
    "crm.agency_defaults.price_checks_per_minute_min": (10, 600),
    "crm.agency_defaults.price_checks_per_minute_max": (10, 600),
    "crm.agency_defaults.trash_days": (1, 30),
    "crm.agency_defaults.trash_days_min": (1, 30),
    "crm.agency_defaults.trash_days_max": (1, 30),
    "crm.agency_defaults.auto_archive_months": (12, 120),
    "crm.agency_defaults.auto_archive_months_min": (12, 120),
    "crm.agency_defaults.auto_archive_months_max": (12, 120),
}


def _rows() -> list[dict]:
    return gen.seed_rows()[0]


def test_compiled_defaults_match_the_migration_seed() -> None:
    assert gen.main(["--check"]) == 0, "run python3 scripts/gen_settings_defaults.py and commit the file"


def test_the_seed_comes_from_064_and_names_its_source() -> None:
    rows, sources = gen.seed_rows()
    assert "064_crm_settings_registry.sql" in sources
    doc = json.loads(gen.OUT.read_text(encoding="utf-8"))
    assert "064_crm_settings_registry.sql" in doc["_generated"]
    assert [r["key"] for r in doc["definitions"]] == sorted(r["key"] for r in rows)


def test_every_seed_row_obeys_the_definition_checks() -> None:
    for r in _rows():
        k = r["key"]
        assert KEY_RE.match(k) and len(k) <= 120, k
        assert r["app"] == "crm", k
        assert 3 <= len(r["label"]) <= 120 and 20 <= len(r["description"]) <= 2000, k
        assert r["value_type"] in VALUE_TYPES, k
        assert r["scope_kind"] in ("platform", "platform+tenant") and r["impact"] in ("high", "medium", "low"), k
        assert r["approval"] in ("single", "maker_checker") and 5 <= r["cache_s"] <= 3600, k
        assert r["env_override"] is None or re.match(r"^[A-Z][A-Z0-9_]{2,63}$", r["env_override"]), k
        lo, hi, d = r["min_value"], r["max_value"], r["default_value"]
        if lo is not None and hi is not None:
            assert lo <= hi, k
        if r["value_type"] in NUMERIC:
            assert isinstance(d, int | float) and not isinstance(d, bool), k
            assert (lo is None or d >= lo) and (hi is None or d <= hi), k
            if r["value_type"] in ("int", "duration_s"):
                assert float(d).is_integer(), k
        elif r["value_type"] == "bool":
            assert isinstance(d, bool), k


def test_bounds_stay_inside_the_agency_column_checks() -> None:
    by_key = {r["key"]: r for r in _rows()}
    for key, (lo, hi) in COLUMN_BOUNDS.items():
        r = by_key[key]
        assert r["min_value"] is not None and r["max_value"] is not None, key
        assert lo <= r["min_value"] <= r["max_value"] <= hi, key


def test_the_two_person_flag_is_maker_checker_and_off() -> None:
    flag = {r["key"]: r for r in _rows()}["crm.settings.two_person_approval"]
    assert (flag["value_type"], flag["approval"], flag["default_value"]) == ("bool", "maker_checker", False)


def test_dry_run_switches_have_no_env_override() -> None:
    # tech lead S279 Q7: Terraform keeps a separate hard never-delete floor; the switch itself is never env-overridden
    for r in _rows():
        if r["area"] == "retention":
            assert r["value_type"] == "bool" and r["env_override"] is None, r["key"]
            assert r["approval"] == "maker_checker", r["key"]
