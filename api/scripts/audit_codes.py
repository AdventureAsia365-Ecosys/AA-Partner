"""The audit codes the API writes, read from the code (AA-1129, S285: the access log must word every line).

Walks ``app/`` with ``ast`` and collects the literal ``action`` / ``object_kind`` / ``fields`` / ``reason`` codes of
every ``*.audit(...)`` call and every ``AuditEvent(...)``. A code is resolved from a string literal, a module-level
string constant, a ``StrEnum`` member (``Action.X`` / ``Action.X.value``), an ``a if c else b`` expression, an
f-string whose holes are such expressions, or a tuple of those (``fields``). A refusal through
``app.permissions.Action`` can name any member, so every ``Action`` value is an action code too.

A ``reason`` is often a variable checked against a closed set first (``if reason_code not in LOCK_REASONS``), which the
reader cannot follow. Those sets are read too: every request body field named ``reason`` / ``reason_code`` typed
``Literal[...]`` (found by itself), and the module constants listed in :data:`REASON_SETS`. Every module constant named
``REASONS`` / ``*_REASONS`` must be listed there or in :data:`NOT_AUDIT_REASON_SETS` (the unit test checks it), so a new
closed set of reasons is never silently left without words. The AI actor's stage (``actor_ref`` "ai:<stage>": the
gateway's ``stage=`` of each ``*.call(...)`` and the literal ``ai:*`` refs) is catalogued as ``ai_stages``.

The web keeps the wording of these codes (``web/lib/auditCodes.json`` + the ``log.act.*`` / ``log.obj.*`` / ``field.*``
/ ``why.*`` / ``log.actor.ai.*`` messages). ``tests/unit/test_aa1129_audit_codes_catalogue.py`` fails when the code
writes a code that the catalogue does not list; ``python scripts/audit_codes.py --write`` rewrites the catalogue (then
word the new codes in the web messages, the web unit test checks that).

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
KINDS = ("actions", "object_kinds", "fields", "reasons", "ai_stages")
REASON_FIELDS = frozenset({"reason", "reason_code"})
AI_REF = "ai:"
# Closed sets a ``reason=`` is checked against before it is written: (module, constant, prefix the audit adds, part).
# ``part``: "members" (a tuple / set, or a dict's keys) or "values" (a dict of tuples, flattened).
REASON_SETS: tuple[tuple[str, str, str, str], ...] = (
    ("app.booking.extra_nights", "CLOSE_REASONS", "", "members"),  # extra nights request closed
    ("app.cis.addons", "REASONS", "cis_", "members"),  # routers/addons.py: f"cis_{refused.reason}"
    ("app.cis.tripplanner", "REASONS", "cis_", "members"),  # routers/tripplanner_module.py: f"cis_{exc.reason}"
    ("app.tripplanner.planner", "REASONS", "tp_", "members"),  # routers/tripplanner_module.py: f"tp_{exc.code}"
    ("app.commissions.service", "WRITE_OFF_REASONS", "", "members"),
    ("app.duty.service", "BULK_REASONS", "", "members"),  # duty of care bulk reveal
    ("app.lifecycle.rules", "ARCHIVE_REASONS", "", "members"),
    ("app.lifecycle.rules", "HOLD_REASONS", "", "members"),  # legal hold placed
    ("app.members.mail", "BREAKGLASS_REASONS", "", "members"),  # AA emergency access opened
    ("app.members.recovery", "REASONS", "", "members"),  # owner recovery opened by AA
    ("app.ops.console", "DATA_REQUEST_PURPOSE", "", "members"),  # AA raised an export / deletion request
    ("app.ops.console", "LOCK_REASONS", "", "members"),  # AA locked the agency
    ("app.ops_release.service", "MEMBER_WITHDRAW_REASONS", "", "members"),
    ("app.pipeline", "LOST_REASONS", "lost_", "members"),  # stages.py: f"lost_{lost_reason}"
    ("app.proposals.public", "CHANGE_REASONS", "", "members"),  # the client asked for changes
    ("app.stages", "CANCEL_REASONS", "", "members"),  # trip_cancel
    ("app.stages", "OVERRIDE_REASONS", "override_", "values"),  # stages.py: f"override_{override}"
    ("app.task_kinds", "STAGE_REASONS", "", "members"),  # stage_change
    ("app.travellers.fields", "REVEAL_REASONS", "", "members"),
)
# Module constants named like a reason set that never reach an audit ``reason`` (why, for the reviewer).
NOT_AUDIT_REASON_SETS: dict[tuple[str, str], str] = {
    ("app.aa_llm.errors", "TRUNCATED_STOP_REASONS"): "model stop reasons of a gateway reply",
    ("app.booking.contract", "CANCEL_REASONS"): "the Booking API's cancel reasons (the audit writes the CRM's)",
    ("app.cis.activation", "REASONS"): "CIS activation refusals answered to the caller, not audited",
    ("app.cis.membership_feed", "REASONS"): "the membership inbox's reason_code column (audits write LOCK_EVENTS)",
    ("app.esign.events", "REFUSAL_REASONS"): "signature event detail, not an audit reason",
    ("app.portability.restore_drill", "INCOMPLETE_DOCUMENT_REASONS"): "restore drill result rows, not audited",
    ("app.support.sessions", "END_REASONS"): "the support console's end_reason view field; the end event has none",
}


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
        found = self._named(node, where) if isinstance(node, (ast.Name, ast.Attribute)) else self._composed(node, where)
        if found is None:
            self.unresolved.append(where)
            return []
        return found

    def _named(self, node: ast.Name | ast.Attribute, where: str) -> list[str] | None:
        """A constant (``TABLE``), an enum member (``Action.X``) or its ``.value``."""
        if isinstance(node, ast.Attribute) and node.attr == "value":
            return self.codes(node.value, where)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            key = f"{node.value.id}.{node.attr}"
        else:
            key = node.id if isinstance(node, ast.Name) else ""
        return list(self.consts[key]) if key in self.consts else None

    def _composed(self, node: ast.AST, where: str) -> list[str] | None:
        """``a if c else b``, ``a or b``, ``"x_" + y``, f-strings and tuples of codes."""
        if isinstance(node, ast.IfExp):
            return self.codes(node.body, where) + self.codes(node.orelse, where)
        if isinstance(node, (ast.BoolOp, ast.Tuple, ast.List, ast.Set)):
            items = node.values if isinstance(node, ast.BoolOp) else node.elts
            return [c for v in items for c in self.codes(v, where)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.codes(node.left, where), self.codes(node.right, where)
            return [a + b for a, b in itertools.product(left, right)] if left and right else None
        if isinstance(node, ast.JoinedStr):
            return self._fstring(node, where)
        return None

    def _fstring(self, node: ast.JoinedStr, where: str) -> list[str] | None:
        parts: list[list[str]] = []
        for v in node.values:
            vals = [str(v.value)] if isinstance(v, ast.Constant) else self.codes(getattr(v, "value", None), where)
            if not vals:
                return None
            parts.append(vals)
        return ["".join(p) for p in itertools.product(*parts)]


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


def _imported(tree: ast.Module, by_module: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
    """The constants and enum members a module imports from another ``app`` module (``from app.x import TABLE``)."""
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module in by_module):
            continue
        src = by_module[node.module]
        for alias in node.names:
            name = alias.asname or alias.name
            for key, vals in src.items():
                if key == alias.name or key.startswith(alias.name + "."):
                    out[name + key[len(alias.name) :]] = vals
    return out


ARGS = (("action", "actions"), ("object_kind", "object_kinds"), ("fields", "fields"), ("reason", "reasons"))


def _module_codes(tree: ast.Module, res: _Resolver, rel: Path, out: dict[str, set[str]]) -> None:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_audit_call(node)):
            continue
        args = _audit_args(node)
        for arg, kind in ARGS:
            if args.get(arg) is not None:
                out[kind].update(v for v in res.codes(args[arg], f"{rel}:{node.lineno}:{arg}") if v)


def _module_assigns(tree: ast.Module) -> dict[str, ast.AST]:
    """Module-level ``NAME = <expr>`` (plain or annotated) by name."""
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out[node.target.id] = node.value
    return out


def _set_members(node: ast.AST, part: str) -> list[str]:
    """The strings of a closed set: a tuple / list / set, ``frozenset({...})``, a dict's keys or its tuple values."""
    if isinstance(node, ast.Call) and node.args:
        return _set_members(node.args[0], part)
    if isinstance(node, ast.Dict) and part == "values":
        return [v for item in node.values for v in _literal_strings(item)]
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return _literal_strings(node)


def _is_reason_set(name: str) -> bool:
    return name == "REASONS" or name.endswith("_REASONS")


def _reason_set_codes(assigned: dict[str, dict[str, ast.AST]]) -> tuple[set[str], list[str]]:
    """The codes of :data:`REASON_SETS`, and the problems: a listed set that is gone or not strings, a constant named
    like a reason set in neither list, a :data:`NOT_AUDIT_REASON_SETS` entry that is gone."""
    codes: set[str] = set()
    problems: list[str] = []
    for mod, name, prefix, part in REASON_SETS:
        node = assigned.get(mod, {}).get(name)
        values = _set_members(node, part) if node is not None else []
        if not values:
            problems.append(f"{mod}.{name}: in REASON_SETS but not a set of string codes")
        codes.update(prefix + v for v in values)
    listed = {(m, n) for m, n, _, _ in REASON_SETS} | set(NOT_AUDIT_REASON_SETS)
    problems += [
        f"{mod}.{name}: list it in REASON_SETS (an audit reason) or NOT_AUDIT_REASON_SETS (why not)"
        for mod, names in assigned.items()
        for name in names
        if _is_reason_set(name) and (mod, name) not in listed
    ]
    gone = [(m, n) for m, n in NOT_AUDIT_REASON_SETS if n not in assigned.get(m, {})]
    problems += [f"{m}.{n}: in NOT_AUDIT_REASON_SETS but gone" for m, n in gone]
    return codes, sorted(problems)


def _literal_values(node: ast.AST | None, aliases: dict[str, ast.AST], res: _Resolver) -> list[str]:
    """The values of a ``Literal[...]`` annotation (``| None``, ``Optional[...]``, an alias, ``Literal[CONSTANT]``)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _literal_values(node.left, aliases, res) + _literal_values(node.right, aliases, res)
    if isinstance(node, ast.Name) and isinstance(aliases.get(node.id), ast.Subscript):
        return _literal_values(aliases[node.id], aliases, res)
    if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)):
        return []
    if node.value.id == "Optional":
        return _literal_values(node.slice, aliases, res)
    if node.value.id != "Literal":
        return []
    items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    return [c for item in items for c in res.codes(item, "literal")]


def _literal_reasons(tree: ast.Module, res: _Resolver) -> set[str]:
    """Request body fields ``reason`` / ``reason_code`` typed ``Literal[...]`` (the value the route audits)."""
    aliases = _module_assigns(tree)
    return {
        v
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for item in cls.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id in REASON_FIELDS
        for v in _literal_values(item.annotation, aliases, res)
    }


def _ai_stages(tree: ast.Module, res: _Resolver, rel: Path) -> set[str]:
    """The AI actor's stages: the gateway's ``stage=`` of every ``*.call(...)`` and the literal ``actor_ref="ai:x"``."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if isinstance(node.func, ast.Attribute) and node.func.attr == "call" and "stage" in kw:
            out.update(res.codes(kw["stage"], f"{rel}:{node.lineno}:stage"))
        ref = kw.get("actor_ref")
        if isinstance(ref, ast.Constant) and isinstance(ref.value, str) and ref.value.startswith(AI_REF):
            out.add(ref.value[len(AI_REF) :])
    return out


def _parse(root: Path) -> dict[Path, ast.Module]:
    return {p: ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in _files(root)}


def _module_name(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root.parent).with_suffix("").parts)


def collect(root: Path = APP) -> tuple[dict[str, set[str]], list[str]]:
    """The codes per kind and the call sites whose code could not be read (``path:line:arg``)."""
    trees = _parse(root)
    consts = {p: _module_constants(t) for p, t in trees.items()}
    by_module = {_module_name(p, root): c for p, c in consts.items()}
    out: dict[str, set[str]] = {k: set() for k in KINDS}
    unresolved: list[str] = []
    for path, tree in trees.items():
        res = _Resolver(consts[path], _imported(tree, by_module))
        rel = path.relative_to(root.parent)
        _module_codes(tree, res, rel, out)
        out["ai_stages"] |= _ai_stages(tree, res, rel)
        unresolved.extend(res.unresolved)
        # A Literal of something the reader cannot resolve is simply not a closed set: not reported.
        out["reasons"] |= _literal_reasons(tree, _Resolver(consts[path], _imported(tree, by_module)))
    out["reasons"] |= _reason_set_codes({_module_name(p, root): _module_assigns(t) for p, t in trees.items()})[0]
    # A refusal through app.permissions names any Action member.
    perms = consts.get(root / "permissions.py", {})
    out["actions"].update(v[0] for k, v in perms.items() if k.startswith("Action."))
    return out, sorted(set(unresolved))


def reason_set_problems(root: Path = APP) -> list[str]:
    """What keeps :data:`REASON_SETS` / :data:`NOT_AUDIT_REASON_SETS` from matching the code (the unit test)."""
    trees = _parse(root)
    return _reason_set_codes({_module_name(p, root): _module_assigns(t) for p, t in trees.items()})[1]


def load_catalogue(path: Path = CATALOGUE) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_catalogue(codes: dict[str, set[str]], path: Path = CATALOGUE) -> None:
    old = load_catalogue(path) if path.exists() else {}
    merged = {k: sorted(set(old.get(k, [])) | codes.get(k, set())) for k in KINDS}
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
