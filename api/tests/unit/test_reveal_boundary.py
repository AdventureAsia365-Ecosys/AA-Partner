"""Card CRM-09 R test: **a public route importing ``reveal`` fails CI** (design v3 S6, lane §5.5).

The public / client routes may show a traveller only their own record, through
``app.travellers.self_reveal.traveller_self_reveal``. The agency's generic ``app.travellers.reveal.reveal`` must not be
reachable from them: no module a public router imports (directly or through other ``app`` modules) may be
``app.travellers.reveal`` or import a name ``reveal`` from anywhere.

Public router modules are found, not listed: every module under ``app/routers`` that builds an ``APIRouter`` whose
prefix starts with ``/crm/public`` (or, later, ``/crm/client``), so a new public router is covered the day it lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
FORBIDDEN_MODULE = "app.travellers.reveal"
FORBIDDEN_NAMES = frozenset({"reveal"})
PUBLIC_PREFIXES = ("/crm/public", "/crm/client")


def _sources() -> dict[str, str]:
    out = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = path.read_text(encoding="utf-8")
    return out


def public_router_modules(sources: dict[str, str]) -> list[str]:
    found = []
    for name, source in sources.items():
        if not name.startswith("app.routers."):
            continue
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "APIRouter"
            ):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "prefix"
                    and isinstance(kw.value, ast.Constant)
                    and str(kw.value.value).startswith(PUBLIC_PREFIXES)
                ):
                    found.append(name)
    return sorted(set(found))


def _imports(module: str, source: str, known: set[str]) -> tuple[set[str], list[str]]:
    """``app`` modules imported by ``module`` and every name ``reveal`` it imports (with the line)."""
    mods: set[str] = set()
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names if a.name.startswith("app")}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module.startswith("app"):
            mods.add(node.module)
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full in known:
                    mods.add(full)
                if alias.name in FORBIDDEN_NAMES:
                    names.append(f"{module}:{node.lineno} imports {alias.name}")
    # A package import runs the package's __init__ too, so its parents count.
    for mod in list(mods):
        parts = mod.split(".")
        mods |= {".".join(parts[:i]) for i in range(1, len(parts))}
    return {m for m in mods if m in known}, names


def boundary_offenders(sources: dict[str, str]) -> list[str]:
    known = set(sources)
    offenders: list[str] = []
    for root in public_router_modules(sources):
        seen, stack = set(), [root]
        while stack:
            mod = stack.pop()
            if mod in seen:
                continue
            seen.add(mod)
            deps, names = _imports(mod, sources[mod], known)
            offenders += [f"{root}: {n}" for n in names]
            if mod == FORBIDDEN_MODULE:
                offenders.append(f"{root}: reaches {FORBIDDEN_MODULE}")
            stack += sorted(deps - seen)
    return sorted(set(offenders))


def test_the_public_router_is_found() -> None:
    assert public_router_modules(_sources()) == ["app.routers.public"]


def test_no_public_route_reaches_the_generic_reveal() -> None:
    assert boundary_offenders(_sources()) == []


def test_r_a_public_route_importing_reveal_fails() -> None:
    sources = _sources()
    sources["app.routers.public"] += "\nfrom app.travellers.reveal import reveal  # a reviewer must never see this\n"
    assert boundary_offenders(sources) == [
        "app.routers.public: app.routers.public:" + str(sources["app.routers.public"].count("\n")) + " imports reveal",
        "app.routers.public: reaches app.travellers.reveal",
    ]


def test_r_reaching_reveal_through_a_helper_fails_too() -> None:
    sources = _sources()
    sources["app.intake.public"] += "\nimport app.travellers.reveal\n"
    assert boundary_offenders(sources) == ["app.routers.public: reaches app.travellers.reveal"]
    sources = _sources()
    sources["app.routers.client_portal"] = (
        "from fastapi import APIRouter\n"
        "from app.travellers import reveal\n"
        'router = APIRouter(prefix="/crm/client/v1")\n'
    )
    assert "app.routers.client_portal: app.routers.client_portal:2 imports reveal" in boundary_offenders(sources)


def test_the_self_view_does_not_import_the_generic_reveal() -> None:
    sources = _sources()
    deps, names = _imports("app.travellers.self_reveal", sources["app.travellers.self_reveal"], set(sources))
    assert FORBIDDEN_MODULE not in deps and names == []
