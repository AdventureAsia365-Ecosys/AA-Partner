"""AA-1111 / AA-1166 (S294 hardening): every cookie the CRM API sets or reads carries a browser-enforced name prefix.

The client-host cookies are path-scoped (``/crm/public/v1/...``), so ``__Host-`` (which needs ``Path=/``) does not
fit; ``__Secure-`` makes the browser refuse the cookie unless it was set with ``Secure`` from an HTTPS origin.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.partners import public as partner
from app.portal import public as portal
from app.routers import client_pages

APP = Path(__file__).resolve().parents[2] / "app"
COOKIES = {
    "portal.SESSION_COOKIE": portal.SESSION_COOKIE,
    "partner.SESSION_COOKIE": partner.SESSION_COOKIE,
    "partner.LINK_COOKIE": partner.LINK_COOKIE,
    "client_pages.PROPOSAL_COOKIE": client_pages.PROPOSAL_COOKIE,
    "client_pages.PORTAL_LINK_COOKIE": client_pages.PORTAL_LINK_COOKIE,
}


def test_every_cookie_name_has_the_secure_prefix() -> None:
    assert {k: v for k, v in COOKIES.items() if not v.startswith("__Secure-")} == {}


def test_the_names_are_the_ones_the_web_sets() -> None:
    web = Path(__file__).resolve().parents[3] / "web" / "lib"
    for name, file in (
        (client_pages.PROPOSAL_COOKIE, "clientProposal.ts"),
        (client_pages.PORTAL_LINK_COOKIE, "clientPortal.ts"),
        (partner.LINK_COOKIE, "partners.ts"),
    ):
        assert f'"{name}"' in (web / file).read_text(encoding="utf-8"), (name, file)


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def test_every_set_or_delete_cookie_names_a_prefixed_constant() -> None:
    """Every ``set_cookie`` / ``delete_cookie`` names a prefixed constant and passes ``secure=True``.

    A browser refuses a ``__Secure-`` cookie without ``Secure``, so a clear (``Max-Age=0``) that forgets it is dropped
    and the cookie stays.
    """
    allowed = {"portal.SESSION_COOKIE", "partner.SESSION_COOKIE"}
    seen: list[str] = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"set_cookie", "delete_cookie"}
            ):
                first = ast.unparse(node.args[0]) if node.args else ast.unparse(node.keywords[0].value)
                seen.append(f"{path.name}:{first}")
                assert first in allowed, (path, first)
                secure = [k.value for k in node.keywords if k.arg == "secure"]
                assert len(secure) == 1 and _is_true(secure[0]), (
                    "__Secure- cookies need secure=True on set and delete",
                    f"{path.name}:{node.lineno}",
                )
    assert seen, "no cookie call found: the scan is broken"
