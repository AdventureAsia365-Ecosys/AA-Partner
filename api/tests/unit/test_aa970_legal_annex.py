"""AA-970 (card CRM-37b): scripts/gen_legal_annex.py, the copy of the root DPA Annex 3 public summary the agency app's
public data and sub-processor pages render. Invented text only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_legal_annex.py"
_spec = importlib.util.spec_from_file_location("gen_legal_annex", SCRIPT)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

BLOCK = """### How we handle your data

Invented Harbour Tours keeps its data with a hosting company.

- **Where it is kept.** In an invented region.

### Sub-processors

| Sub-processor | What it does | Your agency's data it receives | Where |
|---|---|---|---|
| Invented Cloud | Hosting | All of it | Nowhere Bay |
"""


def annex(
    block: str = BLOCK, title: str = '"DPA Annex 3: sub-processors (draft v1.2)"', reviewed: str = "2030-05-06"
) -> str:
    return (
        f"---\nid: X\ntitle: {title}\nlast_reviewed: {reviewed}\n---\n\n# Annex\n\nCounsel notes stay out.\n\n"
        f"<!-- public-summary:begin -->\n{block}<!-- public-summary:end -->\n\nAfter the block.\n"
    )


def test_extract_takes_only_the_marked_block_with_version_and_date() -> None:
    data = gen.extract(annex())
    assert data["version"] == "draft v1.2" and data["updated"] == "2030-05-06"
    assert data["markdown"] == BLOCK
    assert "Counsel notes" not in data["markdown"] and "After the block" not in data["markdown"]
    assert data["source"] == {
        "repo": "AdventureAsia365-Ecosys/AA-Ecosys",
        "branch": "master",
        "path": "docs/legal/dpa-annex-3-subprocessors.md",
    }
    assert gen.render(data).endswith("}\n") and json.loads(gen.render(data)) == data


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (annex().replace("<!-- public-summary:end -->", ""), "exactly one"),
        (annex() + "<!-- public-summary:begin -->", "exactly one"),
        (annex(block="\n"), "empty"),
        (annex(title='"DPA Annex 3"'), "version"),
        (annex(reviewed="soon"), "last_reviewed"),
        (annex(block=BLOCK + "Account 123456789012.\n"), "12-digit"),
        (annex(block=BLOCK + "Role arn:aws:iam::x.\n"), "ARN"),
        (annex(block=BLOCK + "See AA-123.\n"), "issue id"),
        (annex(block=BLOCK + "Served from acc1.\n"), "account label"),
    ],
)
def test_extract_refuses_a_block_that_is_missing_or_not_public_safe(text: str, reason: str) -> None:
    with pytest.raises(gen.AnnexError, match=reason):
        gen.extract(text)


def test_end_before_begin_is_refused() -> None:
    text = (
        'title: "A (draft v1.0)"\nlast_reviewed: 2030-01-01\n'
        "<!-- public-summary:end -->\nx\n<!-- public-summary:begin -->\n"
    )
    with pytest.raises(gen.AnnexError, match="before"):
        gen.extract(text)


def test_main_writes_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "web" / "lib" / "legal" / "annex3.generated.json"
    monkeypatch.setattr(gen, "OUT", out)
    monkeypatch.setattr(gen, "API", tmp_path / "api")
    src = tmp_path / "annex.md"
    src.write_text(annex(), encoding="utf-8")
    assert gen.main(["--check", str(src)]) == 1  # nothing written yet
    assert gen.main([str(src)]) == 0 and json.loads(out.read_text())["version"] == "draft v1.2"
    assert gen.main(["--check", str(src)]) == 0
    src.write_text(annex(block=BLOCK.replace("Nowhere Bay", "Elsewhere Cove")), encoding="utf-8")
    assert gen.main(["--check", str(src)]) == 1
    src.write_text("no markers", encoding="utf-8")
    assert gen.main([str(src)]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_the_committed_json_is_a_public_safe_summary() -> None:
    """The file the web ships: it parses, it names its root source, and it holds both pages' headings."""
    data = json.loads(gen.OUT.read_text(encoding="utf-8"))
    assert data["source"]["path"] == "docs/legal/dpa-annex-3-subprocessors.md"
    assert data["version"].startswith("draft v")
    md = data["markdown"]
    assert md.count("### ") == 2 and "| Sub-processor |" in md
    for pattern, what in gen.FORBIDDEN:
        assert not pattern.search(md), what
