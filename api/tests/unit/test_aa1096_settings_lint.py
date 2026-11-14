"""AA-1096 L4 — settings-lint, the CI guard of the CRM settings registry (root audit
docs/audits/2026-11-13-S279-session/thresholds-audit/ section 5.4; tech lead S279: a threshold, limit, cap or policy
value is a setting, never a code literal or an env var alone). The CRM copy of AA-CIS-App
tests/unit/test_s279_settings_lint.py.

1. **No new policy constant in a governed module.** In every module of ``app/`` (:data:`GOVERNED`; S280 widened it
   from the L4 list of payments, intake, lifecycle, rate limits, the AI gateway, proposals pricing, the retention jobs
   and the partner-account clean-up to the whole package, minus the vendored CIS byte copies in :data:`EXCLUDED`,
   which AA-CIS-App lints and ``test_vendored_copies.py`` compares byte for byte), a module-level
   UPPER_CASE constant whose name looks like policy (``*_MAX``, ``*_MIN``, ``*_CAP``, ``*_LIMIT``, ``*_DAYS``,
   ``*_HOURS``, ``*_PCT``, ``*_THRESHOLD``, ``*_USD``, ``*_ATTEMPTS``, ``*_RETRIES``, ...) bound to a number literal
   fails, unless its line carries ``# technical-constant: <reason>`` or it is in the baseline of the constants that
   were there when their module came under the rule (``snapshots/aa1096_settings_lint_baseline.json``: the L4 modules
   on 13/11/2026, the rest of ``app/`` on 14/11/2026; a list that only shrinks). A numeric
   ``os.environ`` / ``getenv`` read under a policy-looking name fails the same way. Fix: add a setting (a crm migration
   seed row + ``app.platform_settings``), or mark a true engineering constant.
2. **No dead settings.** Every compiled key with ``enforced`` true is read by code (its ``app.platform_settings``
   constant is used outside the binding), and every constant names a compiled key.
3. **The baseline only shrinks.** A baseline entry whose constant is gone (or now reads a setting) fails until it is
   removed from the file.

No DB. Regenerate the baseline only to remove entries: ``python -m tests.unit.test_aa1096_settings_lint``.
CI: the job "Settings Lint" (.github/workflows/ci.yml) runs this file as a check of its own; "Unit Tests" runs it too.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from app import platform_settings as ps

API = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "snapshots" / "aa1096_settings_lint_baseline.json"

GOVERNED = ("app",)
# The vendored AA-CIS-App byte copies (aa_llm, ADR 0009 d13; the identity verifier, ADR 0006 §8 rule 9; the settings
# reader, api/settings_reader.lock.json): their owner lints them, and a marker added here would break the byte copy.
EXCLUDED = ("app/aa_llm/", "app/auth/identity_verifier.py", "app/platform_settings/reader.py")
POLICY_NAME = re.compile(
    r"(^|_)(MAX|MIN|CAP|LIMIT|LIMITS|DAYS|HOURS|PCT|PERCENT|THRESHOLD|THRESHOLDS|USD|ATTEMPTS|RETRIES|QUOTA|BUDGET|"
    r"FLOOR|CEILING|TTL)(_|$)"
)
MARK = "# technical-constant:"
BINDING = {API / "app" / "platform_settings" / "__init__.py", API / "app" / "platform_settings" / "reader.py"}


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in GOVERNED:
        p = API / rel
        out.extend([p] if p.suffix == ".py" else sorted(p.rglob("*.py")))
    return [p for p in out if "__pycache__" not in p.parts and not p.relative_to(API).as_posix().startswith(EXCLUDED)]


def _is_number(node: ast.AST | None) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool)


def _env_read(node: ast.AST | None) -> bool:
    """int(os.environ.get(...)) / float(os.getenv(...)) and the like: a numeric env read."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("int", "float")):
        return False
    return any(isinstance(n, ast.Attribute) and n.attr in ("get", "getenv", "environ") for n in ast.walk(node))


def findings() -> list[str]:
    """Every policy-looking numeric constant or numeric env read at module level of a governed file, unmarked."""
    out = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.parse(text).body:
            targets: list[ast.expr] = []
            value = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for t in targets:
                if not (isinstance(t, ast.Name) and t.id.isupper() and POLICY_NAME.search(t.id)):
                    continue
                if not (_is_number(value) or _env_read(value)):
                    continue
                if MARK in lines[node.lineno - 1]:
                    continue
                out.append(f"{path.relative_to(API).as_posix()}:{t.id}")
    return sorted(set(out))


def _baseline() -> list[str]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["constants"]


def test_no_new_policy_constant_in_a_governed_module() -> None:
    new = sorted(set(findings()) - set(_baseline()))
    assert not new, (
        "policy-looking constants with no setting (make each a setting, crm-064 pattern, "
        f"or mark '{MARK} <reason>'): {new}"
    )


def test_the_baseline_only_shrinks() -> None:
    stale = sorted(set(_baseline()) - set(findings()))
    assert not stale, f"remove these from {BASELINE.name} (python -m tests.unit.test_aa1096_settings_lint): {stale}"


def _key_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(ps).items()
        if name.isupper() and isinstance(value, str) and value.startswith(ps.KEY_PREFIX) and value.count(".") >= 2
    }


def test_every_enforced_setting_has_a_reader_and_every_constant_a_key() -> None:
    constants = _key_constants()
    assert set(constants.values()) <= set(ps.keys()), "a constant names no compiled key"
    used: set[str] = set()
    for path in (API / "app").rglob("*.py"):
        if path in BINDING or "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in constants:
                used.add(constants[node.attr])
    dead = [k for k in ps.keys() if ps.definition(k).enforced and k not in used]
    assert not dead, f"settings with no reader in code (wire them, or seed enforced=false with an issue): {dead}"


def test_the_lint_catches_a_new_cap(tmp_path: Path, monkeypatch: object) -> None:
    mod = tmp_path / "app" / "zz_lint"
    mod.mkdir(parents=True)
    (mod / "m.py").write_text(
        "import os\nRUN_CAP_USD = 5.0\nPOOL_SIZE = 3\nWAIT_DAYS = 2  # technical-constant: x\n"
        'RETRY_MAX = int(os.environ.get("ZZ", "3"))\nNAME_MAX = "x"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "API", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(sys.modules[__name__], "GOVERNED", ("app/zz_lint",))  # type: ignore[attr-defined]
    assert findings() == ["app/zz_lint/m.py:RETRY_MAX", "app/zz_lint/m.py:RUN_CAP_USD"]


if __name__ == "__main__":  # rewrite the baseline to the current findings (only to remove entries)
    current = findings()
    BASELINE.parent.mkdir(exist_ok=True)
    BASELINE.write_text(
        json.dumps(
            {
                "_doc": "AA-1096 settings-lint baseline: policy-looking constants of app/ that are not settings yet "
                "(the L4 modules on 13/11/2026, the rest of app/ on 14/11/2026). Only shrinks.",
                "constants": current,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {BASELINE.relative_to(API)} ({len(current)} entries)")
