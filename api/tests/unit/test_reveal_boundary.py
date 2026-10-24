"""Card CRM-09 R test: **a public route importing ``reveal`` fails CI** (design v3 S6, lane §5.5).

The public / client routes may show a traveller only their own record, through
``app.travellers.self_reveal.traveller_self_reveal``. The agency's generic ``app.travellers.reveal.reveal`` must not be
reachable from them: no module a public router imports (directly or through other ``app`` modules) may be
``app.travellers.reveal`` or import a name ``reveal`` from anywhere.

Public router modules are found, not listed, three ways, so a new public router is covered the day it lands however
it is built: (1) a module under ``app/routers`` that builds ``APIRouter(prefix=...)`` with a prefix starting with
``/crm/public`` or ``/crm/client``; (2) one that builds ``class_router(<RouteClass>)`` (ADR 0006b) whose route class
prefix starts with them; (3) any imported ``app.routers`` module whose ``APIRouter`` objects carry a route under them.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

from fastapi import APIRouter

import app.routers
from app.auth import identity_config

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


def public_route_classes() -> set[str]:
    """Names of the ``identity_config`` route classes whose prefix is public or client-facing."""
    return {
        name
        for name, value in vars(identity_config).items()
        if isinstance(value, identity_config.RouteClass) and value.prefix.startswith(PUBLIC_PREFIXES)
    }


def live_public_router_modules() -> set[str]:
    """(3) Every ``app.routers`` module that, once imported, holds an ``APIRouter`` with a public or client route."""
    found = set()
    for info in pkgutil.iter_modules(app.routers.__path__, "app.routers."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if isinstance(value, APIRouter) and any(
                str(getattr(r, "path", "")).startswith(PUBLIC_PREFIXES) for r in value.routes
            ):
                found.add(info.name)
    return found


def public_router_modules(sources: dict[str, str], *, live: bool = True) -> list[str]:
    found = []
    classes = public_route_classes()
    for name, source in sources.items():
        if not name.startswith("app.routers."):
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if func == "class_router" and node.args:
                arg = node.args[0]
                if getattr(arg, "id", getattr(arg, "attr", None)) in classes:
                    found.append(name)
                continue
            if func != "APIRouter":
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "prefix"
                    and isinstance(kw.value, ast.Constant)
                    and str(kw.value.value).startswith(PUBLIC_PREFIXES)
                ):
                    found.append(name)
    if live:
        found += [m for m in live_public_router_modules() if m in sources]
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
    # AA-939: the invitation claim route is public too (no token: the person has no account yet).
    assert public_router_modules(_sources()) == ["app.routers.invitations", "app.routers.public"]
    assert live_public_router_modules() == {"app.routers.invitations", "app.routers.public"}


def test_a_class_router_with_a_client_route_class_is_a_root(monkeypatch) -> None:  # noqa: ANN001
    """CRM-24a will build its client router the ADR 0006b way; that router must count as a root too."""
    client = identity_config.RouteClass(name="client", prefix="/crm/client/v1", accepts=frozenset())
    monkeypatch.setattr(identity_config, "CLIENT", client, raising=False)
    sources = _sources()
    sources["app.routers.client_portal"] = (
        "from app.auth.identity_config import CLIENT\n"
        "from app.auth.route_guard import class_router\n"
        "from app.travellers.reveal import reveal\n"
        "router = class_router(CLIENT, tags=['client'])\n"
    )
    assert "app.routers.client_portal" in public_router_modules(sources, live=False)
    assert "app.routers.client_portal: reaches app.travellers.reveal" in boundary_offenders(sources)


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
