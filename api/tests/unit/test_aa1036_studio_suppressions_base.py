"""AA-1036 (R3 slice 1) — ``.github/scripts/studio_suppressions_base.py``: the base of the web's ESLint suppressions
growth check gets the portal entries of the files that moved into ``web/`` (under their new paths), never for a path
the base ``web/`` tree already has, and Web Build runs it before ``check-kit-copy.mjs --suppressions``."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / ".github" / "scripts" / "studio_suppressions_base.py"
CI = REPO / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("studio_suppressions_base", SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

BTN = "aa-kit/no-hand-styled-button"


def test_moved_paths() -> None:
    assert mod.moved_path("app/portal/_components/ui.tsx") == "app/(studio)/portal/_components/ui.tsx"
    assert mod.moved_path("app/portal-access/none/page.tsx") == "app/(studio)/portal-access/none/page.tsx"
    assert mod.moved_path("app/terms/page.tsx") == "app/(studio)/legal/partner-terms/page.tsx"
    assert mod.moved_path("app/privacy/page.tsx") == "app/(studio)/legal/privacy/page.tsx"
    assert mod.moved_path("app/_kit/local/PortalSidebar.tsx") == "app/_kit/local/studio/PortalSidebar.tsx"
    assert mod.moved_path("app/tenant-login/page.tsx") is None  # not carried over (root ADR 0019 d6)
    assert mod.moved_path("app/terms/other.tsx") is None


def test_merge_adds_only_files_the_base_web_tree_lacks() -> None:
    web = {"components/A.tsx": {BTN: {"count": 2}}}
    portal = {
        "app/portal/_components/ui.tsx": {BTN: {"count": 5}},
        "app/_kit/local/PortalShell.tsx": {BTN: {"count": 1}},
        "app/tenant-login/page.tsx": {BTN: {"count": 1}},
    }
    out = mod.merged_base(web, portal, {"components/A.tsx", "app/_kit/local/studio/PortalShell.tsx"})
    assert out == {
        "components/A.tsx": {BTN: {"count": 2}},
        "app/(studio)/portal/_components/ui.tsx": {BTN: {"count": 5}},
    }
    # the base web file wins for a path it already lists (after the move merged, nothing is added)
    again = mod.merged_base(out, portal, set(out) | {"app/_kit/local/studio/PortalShell.tsx"})
    assert again == out
    assert web == {"components/A.tsx": {BTN: {"count": 2}}}  # inputs untouched


def test_main_writes_the_merged_file(tmp_path: Path) -> None:
    (tmp_path / "web.json").write_text(json.dumps({}))
    (tmp_path / "portal.json").write_text(json.dumps({"app/portal/x.tsx": {BTN: {"count": 3}}}))
    (tmp_path / "files.txt").write_text("app/layout.tsx\n\n")
    out = tmp_path / "out.json"
    argv = ["s", str(tmp_path / "web.json"), str(tmp_path / "portal.json"), str(tmp_path / "files.txt"), str(out)]
    assert mod.main(argv) == 0
    assert json.loads(out.read_text()) == {"app/(studio)/portal/x.tsx": {BTN: {"count": 3}}}
    # no portal file at the base (after R3 slice 2): the base web file as it is
    assert mod.main([*argv[:2], "-", *argv[3:]]) == 0
    assert json.loads(out.read_text()) == {}
    assert mod.main(["s"]) == 2


def test_web_build_merges_the_studio_base_before_the_growth_check() -> None:
    ci = CI.read_text(encoding="utf-8")
    step = ci[ci.index("- name: Suppressions do not grow") :]
    step = step[: step.index("- name: No inline disable of a kit rule")]
    assert "studio_suppressions_base.py" in step
    assert 'git show "$BASE_SHA:portal/eslint-suppressions.json"' in step
    assert step.index("studio_suppressions_base.py") < step.index("check-kit-copy.mjs --suppressions")
