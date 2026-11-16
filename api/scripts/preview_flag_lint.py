#!/usr/bin/env python3
"""Preview-flag lint (AA-970, design v3 card CRM-37b, R42): privacy or price logic never sits behind a preview flag.

An agency is "on preview" when its own value of the settings key ``crm.release.preview`` is on (crm-072); the web then
shows the preview features of ``web/lib/preview.ts`` (``previewOn(me, flag)``, ``/crm/v1/me`` ``preview``). A preview
is a UI channel for trying new screens early. A change to who sees personal data, to retention, erasure or consent, or
to a price, a payment, a margin or a commission must reach every agency at once, so this lint fails when:

1. **registry** — a flag of ``web/lib/preview.ts`` (an ``area: "<area>"`` entry, or a member of the ``PreviewArea``
   union) names a privacy or money area;
2. **web path** — a web file (``web/app``, ``web/components``, ``web/lib``) that calls the gate (``previewOn(``,
   ``previewFeatures(``, ``PREVIEW_FEATURES``) lives in a privacy or money part of the app (its path names one);
3. **web import** — such a file imports a privacy or money module;
4. **api path / import** — an API module (``api/app``) that reads the switch (``RELEASE_PREVIEW`` or
   ``"crm.release.preview"``) lives in, or imports, a privacy or money module.

Fix: keep the preview part in a component or module of its own that holds no such logic, or ship the change to every
agency. Run from the repo root or ``api/``: ``python3 scripts/preview_flag_lint.py`` (exit 1 on a finding).
``tests/unit/test_aa970_preview_lint.py`` runs it on the repo in the Unit Tests job and proves each rule catches what
it is for. Standard library only.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REGISTRY = Path("web/lib/preview.ts")
WEB_DIRS = ("web/app", "web/components", "web/lib")
API_DIR = "api/app"

# The words of a privacy or money area, matched against path segments, module names and area names (lower case,
# separators "-", "_", "/", "." and camelCase humps split). "legal" is a path word only: lib/studio/legal.ts holds the
# public page paths, which any page may link to.
PRIVACY_WORDS = (
    "privacy", "consent", "erase", "erasure", "eraser", "retention", "lifecycle", "portability", "vault", "reveal",
    "pii", "scrub", "scrubber", "access", "audit", "gdpr", "dsar",
)  # fmt: skip
MONEY_WORDS = (
    "price", "prices", "pricing", "tariff", "payment", "payments", "invoice", "invoices", "refund", "refunds", "margin",
    "commission", "commissions", "finance", "money", "markup", "discount", "cancellation", "cancel", "deposit",
    "currency", "fx", "ledger", "bill", "bills", "booking", "bookings",
)  # fmt: skip
FORBIDDEN = frozenset(PRIVACY_WORDS + MONEY_WORDS)
PATH_ONLY = frozenset({"legal"})
# Parts of the web whose words are too plain for the list above (Settings > Data and privacy is "settings/data").
FORBIDDEN_WEB_DIRS = ("web/app/(crm)/settings/data/", "web/components/settings/Data")

GATE_TS = re.compile(r"\b(previewOn|previewFeatures)\s*\(|\bPREVIEW_FEATURES\b")
IMPORT_TS = re.compile(
    r"""(?:^|\n)\s*(?:import|export)\b[^'"]*?from\s*['"]([^'"]+)['"]|\bimport\(\s*['"]([^'"]+)['"]\s*\)"""
)
AREA_TS = re.compile(r"""\barea\s*:\s*['"]([^'"]+)['"]""")
UNION_TS = re.compile(r"export\s+type\s+PreviewArea\s*=([^;]+);", re.S)
GATE_PY = re.compile(r"\bRELEASE_PREVIEW\b|['\"]crm\.release\.preview['\"]")


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.rule}] {self.detail}"


def words(text: str) -> set[str]:
    """The lower-case words of a path, module name or area: split on separators and camelCase humps."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {w for w in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if w}


def forbidden_in(text: str, *, path: bool) -> list[str]:
    allowed = FORBIDDEN | (PATH_ONLY if path else frozenset())
    return sorted(words(text) & allowed)


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def check_registry(root: Path) -> list[Finding]:
    reg = root / REGISTRY
    if not reg.is_file():
        return [Finding("registry", REGISTRY.as_posix(), "the preview registry is missing")]
    text = reg.read_text(encoding="utf-8")
    areas = AREA_TS.findall(text)
    union = UNION_TS.search(text)
    if union:
        areas += re.findall(r"""['"]([^'"]+)['"]""", union.group(1))
    out = []
    for area in sorted(set(areas)):
        hits = forbidden_in(area, path=True)
        if hits:
            out.append(
                Finding(
                    "registry", REGISTRY.as_posix(), f"preview area {area!r} is privacy or money ({', '.join(hits)})"
                )
            )
    return out


def _ts_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for d in WEB_DIRS:
        base = root / d
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.suffix in (".ts", ".tsx") and "node_modules" not in p.parts]
    return sorted(files)


def check_web(root: Path) -> list[Finding]:
    out = []
    for p in _ts_files(root):
        rel = _rel(root, p)
        if rel == REGISTRY.as_posix():
            continue
        text = p.read_text(encoding="utf-8")
        if not GATE_TS.search(text):
            continue
        hits = forbidden_in(rel.removeprefix("web/"), path=True)
        hits += [d for d in FORBIDDEN_WEB_DIRS if rel.startswith(d)]
        if hits:
            out.append(
                Finding(
                    "web-path", rel, f"calls the preview gate in a privacy or money part of the app ({', '.join(hits)})"
                )
            )
        for m in IMPORT_TS.finditer(text):
            spec = m.group(1) or m.group(2)
            hits = forbidden_in(spec, path=False)
            if hits:
                out.append(
                    Finding("web-import", rel, f"calls the preview gate and imports {spec!r} ({', '.join(hits)})")
                )
    return out


def _py_imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names += [f"{node.module}.{a.name}" for a in node.names]
    return names


def check_api(root: Path) -> list[Finding]:
    out = []
    base = root / API_DIR
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        if not GATE_PY.search(text):
            continue
        rel = _rel(root, p)
        hits = forbidden_in(rel.removeprefix("api/"), path=True)
        if hits:
            out.append(
                Finding("api-path", rel, f"reads the preview switch in a privacy or money module ({', '.join(hits)})")
            )
        for name in _py_imports(ast.parse(text, filename=rel)):
            if not name.startswith("app."):
                continue
            hits = forbidden_in(name.removeprefix("app."), path=False)
            if hits:
                out.append(
                    Finding("api-import", rel, f"reads the preview switch and imports {name!r} ({', '.join(hits)})")
                )
    return out


def lint(root: Path = REPO) -> list[Finding]:
    return check_registry(root) + check_web(root) + check_api(root)


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]).resolve() if argv else REPO
    findings = lint(root)
    for f in findings:
        print(f"FAIL  {f}")
    print(f"preview-flag lint: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
