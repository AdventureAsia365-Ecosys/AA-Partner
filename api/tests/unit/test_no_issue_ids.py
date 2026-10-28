"""AA-994 guard: no string the API can send (an HTTP detail, a response value, a seeded label) carries an internal
Linear issue ID (``AA-123`` style). Issue IDs belong in comments, docstrings, tests and commit text only.

Every string constant under app/ is checked (f-string parts included), except docstrings and bare expression strings,
which never leave the process. Comments are not tokens, so they are never seen.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ISSUE_ID = re.compile(r"\bAA-\d+\b")


def _strings(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))  # docstrings and bare string statements
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and ISSUE_ID.search(node.value)
    ]


def test_no_string_in_app_contains_an_issue_id() -> None:
    files = sorted(APP.rglob("*.py"))
    assert len(files) > 50, f"expected the app sources, found {len(files)} files"
    hits = [
        f"{path.relative_to(APP.parent)}:{line}: {text[:120]!r}"
        for path in files
        for line, text in _strings(path.read_text(encoding="utf-8"))
    ]
    assert hits == [], "move the issue ID into a comment:\n" + "\n".join(hits)


def test_the_scanner_sees_strings_and_f_strings_but_not_docstrings_or_comments() -> None:
    sample = '''"""Module AA-1 docstring."""
# AA-2 comment
def f(x):
    """AA-3 function docstring."""
    raise ValueError("bad input (AA-4)")
DETAIL = f"see {1} AA-5"
LABEL = {"name": "AA-6"}
'''
    assert sorted(text for _, text in _strings(sample)) == sorted(["bad input (AA-4)", " AA-5", "AA-6"])
