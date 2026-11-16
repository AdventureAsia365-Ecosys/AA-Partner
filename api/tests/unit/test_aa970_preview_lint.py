"""AA-970 (design v3 card CRM-37b, R42) — R: privacy or price logic behind a preview flag fails a lint.

``scripts/preview_flag_lint.py`` runs on the repo here (the Unit Tests job, so CI goes red on a finding), and each
rule is shown to catch what it is for on a small invented tree."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "preview_flag_lint.py"
_spec = importlib.util.spec_from_file_location("preview_flag_lint", SCRIPT)
assert _spec and _spec.loader
lint = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lint  # the dataclass looks its module up while the file runs
_spec.loader.exec_module(lint)

REGISTRY_OK = """export type PreviewArea = "help" | "pipeline";
export const PREVIEW_FEATURES = { "help.panel": { area: "help", since: "2030-01-01" } };
export function previewOn() { return false; }
"""
GATE = 'import { previewOn } from "@/lib/preview";\nexport const shown = previewOn(me, "help.panel");\n'


def tree(tmp_path: Path, files: dict[str, str]) -> Path:
    base = {"web/lib/preview.ts": REGISTRY_OK, "api/app/__init__.py": ""}
    for rel, text in (base | files).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def rules(root: Path) -> list[str]:
    return [f.rule for f in lint.lint(root)]


def test_the_repo_has_no_preview_flag_on_privacy_or_price_logic() -> None:
    findings = lint.lint()
    assert findings == [], "\n".join(map(str, findings))


def test_the_repo_really_uses_the_gate_and_the_switch() -> None:
    """The clean run above means something: the gate and the switch are used where the lint looks."""
    web = [p for p in lint._ts_files(lint.REPO) if lint.GATE_TS.search(p.read_text(encoding="utf-8"))]
    assert any(p.as_posix().endswith("app/(crm)/help/page.tsx") for p in web)
    api = (lint.REPO / "api/app/routers/agency.py").read_text(encoding="utf-8")
    assert lint.GATE_PY.search(api)


def test_a_clean_tree_passes(tmp_path: Path) -> None:
    root = tree(
        tmp_path, {"web/app/(crm)/help/page.tsx": GATE, "web/app/(crm)/clients/page.tsx": "export const x = 1;\n"}
    )
    assert rules(root) == []
    assert lint.main([str(root)]) == 0


@pytest.mark.parametrize("area", ["privacy", "pricing", "payments", "data-retention", "aaTariff", "commissions"])
def test_a_flag_in_a_privacy_or_money_area_fails(tmp_path: Path, area: str) -> None:
    registry = REGISTRY_OK.replace('area: "help"', f'area: "{area}"')
    assert rules(tree(tmp_path, {"web/lib/preview.ts": registry})) == ["registry"]


def test_a_forbidden_area_in_the_type_union_fails(tmp_path: Path) -> None:
    registry = REGISTRY_OK.replace('"help" | "pipeline"', '"help" | "pricing"')
    assert rules(tree(tmp_path, {"web/lib/preview.ts": registry})) == ["registry"]


def test_a_missing_registry_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, {})
    (root / "web/lib/preview.ts").unlink()
    assert rules(root) == ["registry"]


@pytest.mark.parametrize(
    "path",
    [
        "web/app/(crm)/settings/data/page.tsx",
        "web/app/(crm)/settings/pricing/page.tsx",
        "web/app/(crm)/payments/page.tsx",
        "web/app/(crm)/commissions/page.tsx",
        "web/components/settings/DataLifecycleView.tsx",
        "web/lib/finance.ts",
        "web/lib/retailPricing.ts",
        "web/app/(studio)/legal/privacy/page.tsx",
    ],
)
def test_the_gate_in_a_privacy_or_money_part_of_the_web_fails(tmp_path: Path, path: str) -> None:
    assert "web-path" in rules(tree(tmp_path, {path: GATE}))


@pytest.mark.parametrize(
    "module", ["@/lib/finance", "@/lib/retailPricing", "@/lib/portability", "../privacy/scrub", "@/lib/aaTariff"]
)
def test_the_gate_beside_a_privacy_or_money_import_fails(tmp_path: Path, module: str) -> None:
    text = f'import {{ x }} from "{module}";\n' + GATE
    assert rules(tree(tmp_path, {"web/app/(crm)/home/Tile.tsx": text})) == ["web-import"]


def test_a_dynamic_import_counts_and_the_public_page_paths_do_not(tmp_path: Path) -> None:
    dynamic = GATE + 'const m = await import("@/lib/margin");\n'
    assert rules(tree(tmp_path / "a", {"web/app/(crm)/home/Tile.tsx": dynamic})) == ["web-import"]
    links = 'import { DATA_PATH } from "@/lib/studio/legal";\n' + GATE
    assert rules(tree(tmp_path / "b", {"web/app/(crm)/home/Links.tsx": links})) == []


@pytest.mark.parametrize(
    ("path", "text", "rule"),
    [
        (
            "api/app/privacy/scrubber.py",
            "from app import platform_settings as ps\nX = ps.RELEASE_PREVIEW\n",
            "api-path",
        ),
        ("api/app/payments/refunds.py", "KEY = 'crm.release.preview'\n", "api-path"),
        ("api/app/routers/home.py", "from app.margin import serialize\nX = RELEASE_PREVIEW\n", "api-import"),
        ("api/app/routers/home.py", "import app.vault.keys\nX = RELEASE_PREVIEW\n", "api-import"),
        ("api/app/routers/home.py", "from app.fx import rates\nX = RELEASE_PREVIEW\n", "api-import"),
    ],
)
def test_the_switch_in_or_beside_privacy_or_money_api_code_fails(
    tmp_path: Path, path: str, text: str, rule: str
) -> None:
    root = tree(tmp_path, {path: text})
    assert rule in rules(root)
    assert lint.main([str(root)]) == 1


def test_api_code_that_does_not_read_the_switch_is_left_alone(tmp_path: Path) -> None:
    root = tree(tmp_path, {"api/app/privacy/scrubber.py": "from app.vault import keys\n"})
    assert rules(root) == []


def test_words_split_paths_modules_and_camel_case() -> None:
    assert lint.words("@/lib/retailPricing") >= {"retail", "pricing"}
    assert lint.words("app/(crm)/aa-tariff/page.tsx") >= {"aa", "tariff"}
    assert lint.forbidden_in("@/lib/studio/legal", path=False) == []
    assert lint.forbidden_in("web/app/legal/x.tsx", path=True) == ["legal"]
