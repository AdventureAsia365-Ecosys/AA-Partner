"""AA-957 (S262 live evidence): the trip page showed raw codes ("stage3_due_t21", "collect_deposit", "aa_incident",
"deposit_paid -> Booked") because nothing tied the codes the API writes to the labels the web has.
``app.task_kinds`` is that list; this test reads app/ and keeps it complete in both directions (the web test
``web/tests/tripCodes.test.ts`` then checks every listed code has an EN and a VI label).

What counts as "written":

- a task kind: a string constant bound to a ``*_TASK`` / ``TASK`` name, a literal kind in an ``INSERT INTO crm.task``
  SQL text, or a string literal passed as the kind (third argument) of a call that runs a ``*TASK*`` SQL text;
- a stage reason: a literal reason in an ``INSERT INTO crm.trip_stage_history`` SQL text, a ``reason=`` literal handed
  to ``move()``, a literal reason (fifth argument) of a call that runs ``HISTORY_SQL``, or a string (or f-string
  prefix) assigned to a variable called ``reason`` in the stage modules.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.task_kinds import STAGE_REASON_PREFIXES, STAGE_REASONS, TASK_KINDS

APP = Path(__file__).resolve().parents[2] / "app"
CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TASK_INSERT = re.compile(
    r"INSERT INTO crm\.task\b.*?(?:VALUES\s*\(\s*\$1,\s*|SELECT\s+\$1,\s*)'([a-z][a-z0-9_]*)'", re.S
)
HISTORY_INSERT = re.compile(r"INSERT INTO crm\.trip_stage_history\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", re.S)
# The modules that pick a stage reason in a variable (``reason = ...`` / ``to, reason = ..., ...``).
REASON_MODULES = ("stages.py", "stages_job.py")


def _trees() -> list[tuple[str, ast.Module]]:
    return [(str(p.relative_to(APP)), ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(APP.rglob("*.py"))]


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _strings(node: ast.AST | None) -> list[str]:
    """String constants a value can take: a constant, both arms of ``a if c else b``, and the leading text of an
    f-string (a reason family such as ``lost_``)."""
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _strings(node.body) + _strings(node.orelse)
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        return [head.value] if isinstance(head, ast.Constant) and isinstance(head.value, str) else []
    if isinstance(node, ast.Subscript):  # f"lost_{x}"[:40]
        return _strings(node.value)
    return []


def _str_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) > index:
        arg = call.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def written_task_kinds() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for where, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if re.search(r"(^|_)TASK$", node.targets[0].id):
                    for s in _strings(node.value):
                        found.setdefault(s, set()).add(f"{where}:{node.targets[0].id}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in TASK_INSERT.finditer(node.value):
                    found.setdefault(m.group(1), set()).add(f"{where}:sql")
            if isinstance(node, ast.Call) and node.args and "TASK" in _name(node.args[0]):
                kind = _str_arg(node, 2)
                if kind is not None:
                    found.setdefault(kind, set()).add(f"{where}:{_name(node.args[0])}")
    return found


def written_stage_reasons() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def add(code: str, where: str) -> None:
        found.setdefault(code, set()).add(where)

    for where, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in HISTORY_INSERT.finditer(node.value):
                    columns = [c.strip() for c in m.group(1).split(",")]
                    values = [v.strip() for v in m.group(2).split(",")]
                    lit = re.fullmatch(r"'([a-z][a-z0-9_]*)'", values[columns.index("reason_code")])
                    if lit:
                        add(lit.group(1), f"{where}:sql")
            if isinstance(node, ast.Call):
                if _name(node.func) == "move":
                    for kw in node.keywords:
                        if kw.arg == "reason":
                            for s in _strings(kw.value):
                                add(s, f"{where}:move")
                if node.args and _name(node.args[0]) == "HISTORY_SQL":
                    for s in _strings(node.args[4]) if len(node.args) > 4 else []:
                        add(s, f"{where}:HISTORY_SQL")
            if where in REASON_MODULES and isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == "reason":
                    for s in _strings(node.value):
                        add(s, f"{where}:reason")
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    for t, v in zip(target.elts, node.value.elts, strict=False):
                        if isinstance(t, ast.Name) and t.id == "reason":
                            for s in _strings(v):
                                add(s, f"{where}:reason")
    return found


def _listed_reason(code: str) -> bool:
    return code in STAGE_REASONS or any(code.startswith(p) for p in STAGE_REASON_PREFIXES)


def test_every_task_kind_the_api_writes_is_listed_and_every_listed_kind_is_written() -> None:
    written = written_task_kinds()
    assert all(CODE.match(k) for k in written), sorted(written)
    unlisted = {k: sorted(v) for k, v in written.items() if k not in TASK_KINDS}
    assert not unlisted, f"add these task kinds to app/task_kinds.py (and their web labels): {unlisted}"
    stale = sorted(TASK_KINDS - set(written))
    assert not stale, f"no module writes these kinds any more; drop them from app/task_kinds.py: {stale}"


def test_every_stage_reason_the_api_writes_is_listed_and_every_listed_reason_is_written() -> None:
    written = written_stage_reasons()
    assert all(CODE.match(c) for c in written), sorted(written)
    unlisted = {c: sorted(v) for c, v in written.items() if not _listed_reason(c)}
    assert not unlisted, f"add these stage reasons to app/task_kinds.py (and their web labels): {unlisted}"
    stale = sorted(STAGE_REASONS - set(written))
    assert not stale, f"no module writes these reasons any more; drop them from app/task_kinds.py: {stale}"
    # Both reason families are really written (the lost menu and the cancel).
    assert {p for p in STAGE_REASON_PREFIXES if p in written} == set(STAGE_REASON_PREFIXES)


def test_the_scan_sees_the_shapes_it_relies_on() -> None:
    # The kinds of the live evidence come from three different shapes: a literal in an execute call (stages_job), a
    # literal in the INSERT text (proposals/public) and a *_TASK constant (booking/sync).
    kinds = written_task_kinds()
    assert "stages_job.py:TASK_SQL" in kinds["stage3_due_t21"]
    assert "proposals/public.py:sql" in kinds["collect_deposit"]
    assert "booking/sync.py:INCIDENT_TASK" in kinds["aa_incident"]
    reasons = written_stage_reasons()
    assert "proposals/service.py:move" in reasons["deposit_paid"]
    assert "imports/engine.py:sql" in reasons["import"]
    assert "stages_job.py:reason" in reasons["date_rule_gate_unmet"]
