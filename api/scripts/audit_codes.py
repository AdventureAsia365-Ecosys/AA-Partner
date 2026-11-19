"""The audit codes the API writes, read from the code (AA-1129, S285: the access log must word every line).

Walks ``app/`` with ``ast`` and collects the literal ``action`` / ``object_kind`` / ``fields`` / ``reason`` codes of
every ``*.audit(...)`` call and every ``AuditEvent(...)``. A code is resolved from a string literal, a module-level
string constant, a ``StrEnum`` member (``Action.X`` / ``Action.X.value``), an ``a if c else b`` expression, an
f-string whose holes are such expressions, or a tuple of those (``fields``). A refusal through
``app.permissions.Action`` can name any member, so every ``Action`` value is an action code too.

The web keeps the wording of these codes (``web/lib/auditCodes.json`` + the ``log.act.*`` / ``log.obj.*`` / ``field.*``
/ ``why.*`` messages). ``tests/unit/test_aa1129_audit_codes_catalogue.py`` fails when the code writes a code that the
catalogue does not list; ``python scripts/audit_codes.py --write`` rewrites the catalogue (then word the new codes in
the web messages, the web unit test checks that).

Usage (from ``api/``): ``python scripts/audit_codes.py`` prints the codes as JSON; ``--write`` updates the catalogue.
"""

from __future__ import annotations

import ast
import itertools
import json
import sys
from collections.abc import Iterable
from pathlib import Path

API = Path(__file__).resolve().parents[1]
APP = API / "app"
CATALOGUE = API.parent / "web" / "lib" / "auditCodes.json"
OUTCOMES = {"allowed", "denied"}
KINDS = ("actions", "object_kinds", "fields", "reasons")


def _module_constants(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level ``NAME = "code"`` / ``NAME = ("a", "b")`` and ``class X(StrEnum)`` members (``X.M``)."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            vals = _literal_strings(node.value)
            if vals:
                out[node.targets[0].id] = vals
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            vals = _literal_strings(node.value)
            if vals:
                out[node.target.id] = vals
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                ):
                    out[f"{node.name}.{item.targets[0].id}"] = [item.value.value]
    return out


def _literal_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return [e.value for e in node.elts]  # type: ignore[attr-defined]
    return []


class _Resolver:
    def __init__(self, consts: dict[str, list[str]], imported: dict[str, list[str]]) -> None:
        self.consts = {**imported, **consts}
        self.unresolved: list[str] = []

    def codes(self, node: ast.AST | None, where: str) -> list[str]:
        """Every string the expression can take, or [] (recorded as unresolved) when it is not a known shape."""
        if node is None:
            return []
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.Name) and node.id in self.consts:
            return list(self.consts[node.id])
        if isinstance(node, ast.Attribute):
            if node.attr == "value":
                return self.codes(node.value, where)
            if isinstance(node.value, ast.Name) and f"{node.value.id}.{node.attr}" in self.consts:
                return list(self.consts[f"{node.value.id}.{node.attr}"])
        if isinstance(node, ast.IfExp):
            return self.codes(node.body, where) + self.codes(node.orelse, where)
        if isinstance(node, ast.BoolOp):
            return [c for v in node.values for c in self.codes(v, where)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.codes(node.left, where), self.codes(node.right, where)
            if left and right:
                return [a + b for a, b in itertools.product(left, right)]
        if isinstance(node, ast.JoinedStr):
            parts: list[list[str]] = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append([str(v.value)])
                elif isinstance(v, ast.FormattedValue):
                    vals = self.codes(v.value, where)
                    if not vals:
                        self.unresolved.append(where)
                        return []
                    parts.append(vals)
            return ["".join(p) for p in itertools.product(*parts)]
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [c for e in node.elts for c in self.codes(e, where)]
        self.unresolved.append(where)
        return []


def _audit_args(call: ast.Call) -> dict[str, ast.AST | None]:
    """Map a call's arguments to action / object_kind / fields / reason (both ``ctx.audit`` shapes + AuditEvent)."""
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    func = call.func
    if isinstance(func, ast.Name) and func.id == "AuditEvent":
        names = ["actor_kind", "actor_ref", "action", "outcome", "object_kind", "object_id", "fields", "reason"]
        pos = dict(zip(names, call.args, strict=False))
        pick = {**pos, **kw}
        return {k: pick.get(k) for k in ("action", "object_kind", "fields", "reason")}
    args = call.args
    second = args[1] if len(args) > 1 else kw.get("outcome")
    # The second argument is a literal that is not an outcome: the platform shape has no outcome.
    platform_shape = isinstance(second, ast.Constant) and isinstance(second.value, str) and second.value not in OUTCOMES
    if platform_shape:  # PlatformContext.audit(action, object_kind, object_id, *, fields, object_ids)
        return {
            "action": args[0] if args else kw.get("action"),
            "object_kind": second,
            "fields": kw.get("fields"),
            "reason": None,
        }
    return {
        "action": args[0] if args else kw.get("action"),
        "object_kind": args[2] if len(args) > 2 else kw.get("object_kind"),
        "fields": kw.get("fields"),
        "reason": kw.get("reason"),
    }


def _is_audit_call(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "audit") or (
        isinstance(f, ast.Name) and f.id in ("audit", "AuditEvent")
    )


def _files(root: Path) -> Iterable[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def collect(root: Path = APP) -> tuple[dict[str, set[str]], list[str]]:
    """The codes per kind and the call sites whose code could not be read (``path:line:arg``)."""
    trees = {p: ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in _files(root)}
    consts = {p: _module_constants(t) for p, t in trees.items()}
    by_module = {".".join(p.relative_to(root.parent).with_suffix("").parts): c for p, c in consts.items()}
    out: dict[str, set[str]] = {k: set() for k in KINDS}
    unresolved: list[str] = []
    for path, tree in trees.items():
        imported: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in by_module:
                src = by_module[node.module]
                for alias in node.names:
                    name = alias.asname or alias.name
                    for key, vals in src.items():
                        if key == alias.name:
                            imported[name] = vals
                        elif key.startswith(alias.name + "."):
                            imported[name + key[len(alias.name) :]] = vals
        res = _Resolver(consts[path], imported)
        rel = path.relative_to(root.parent)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_audit_call(node)):
                continue
            args = _audit_args(node)
            for arg, kind in (
                ("action", "actions"),
                ("object_kind", "object_kinds"),
                ("fields", "fields"),
                ("reason", "reasons"),
            ):
                if args.get(arg) is None:
                    continue
                vals = res.codes(args[arg], f"{rel}:{node.lineno}:{arg}")
                out[kind].update(v for v in vals if v)
        unresolved.extend(res.unresolved)
    # A refusal through app.permissions names any Action member.
    perms = consts.get(root / "permissions.py", {})
    out["actions"].update(v[0] for k, v in perms.items() if k.startswith("Action."))
    return out, sorted(set(unresolved))


def load_catalogue(path: Path = CATALOGUE) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_catalogue(codes: dict[str, set[str]], path: Path = CATALOGUE) -> None:
    old = load_catalogue(path) if path.exists() else {}
    merged = {k: sorted(set(old.get(k, [])) | codes[k]) for k in KINDS}
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    codes, unresolved = collect()
    if "--write" in argv:
        write_catalogue(codes)
    else:
        print(json.dumps({k: sorted(v) for k, v in codes.items()}, indent=2))
    if unresolved:
        print(f"{len(unresolved)} call arguments are not literal codes:", file=sys.stderr)
        for u in unresolved:
            print(f"  {u}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
