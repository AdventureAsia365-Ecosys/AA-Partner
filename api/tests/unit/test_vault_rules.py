"""Static rules for encrypted data (AA-934; errata S247 row CRM-06):

- **Integration secrets are decrypted only where they are used**: ``.decrypt_secret(`` may appear only in the
  dispatcher, webhook and health-check modules (``app/integrations/dispatch*``, ``webhook*``, ``health*``), the mail
  sender (``app/mail/sender.py``, AA-945) and in the vault itself.
- **No route returns ciphertext, an index or a secret**: no response model of any served route (nested models
  included) has a field named ``*_enc``, ``*_bidx``, ``secret*`` or ``wrapped_dek``; no router module names the
  columns ``secret_enc`` or ``wrapped_dek``.
"""

from __future__ import annotations

import ast
import re
import typing
from pathlib import Path

from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.main import create_app
from app.settings import Settings

APP = Path(__file__).resolve().parents[2] / "app"
# AA-945: the mail sender (app/mail/sender.py, the dispatcher's Sender; the health check opens credentials through it).
SECRET_DECRYPT_ALLOWED = re.compile(
    r"^app/(vault/vault\.py|integrations/(dispatch|webhook|health)[a-z_]*\.py|mail/sender\.py)$"
)
FORBIDDEN_FIELD = re.compile(r"(_enc|_bidx)$|^secret|^wrapped_dek$")


def _calls(tree: ast.AST, name: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name
    ]


def secret_decrypt_offenders(files: dict[str, str]) -> list[str]:
    out = []
    for rel, source in files.items():
        if SECRET_DECRYPT_ALLOWED.match(rel):
            continue
        out += [f"{rel}:{line}" for line in _calls(ast.parse(source), "decrypt_secret")]
    return out


def _app_files() -> dict[str, str]:
    return {str(p.relative_to(APP.parent)): p.read_text(encoding="utf-8") for p in sorted(APP.rglob("*.py"))}


def test_integration_secrets_are_decrypted_only_in_the_allowed_paths() -> None:
    assert secret_decrypt_offenders(_app_files()) == []


def test_the_secret_lint_catches_a_route() -> None:
    bad = "async def r(vault, c, b):\n    return await vault.decrypt_secret(c, b)\n"
    assert secret_decrypt_offenders({"app/routers/agency.py": bad}) == ["app/routers/agency.py:2"]
    for allowed in (
        "app/integrations/dispatcher.py",
        "app/integrations/webhooks_twilio.py",
        "app/integrations/health.py",
    ):
        assert secret_decrypt_offenders({allowed: bad}) == []


def _model_fields(model: type[BaseModel], seen: set[type]) -> list[str]:
    if model in seen:
        return []
    seen.add(model)
    names = []
    for name, info in model.model_fields.items():
        names.append(name)
        for arg in [info.annotation, *typing.get_args(info.annotation)]:
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names += _model_fields(arg, seen)
    return names


def forbidden_response_fields(app) -> list[str]:  # noqa: ANN001
    bad = []
    for route in app.routes:
        model = getattr(route, "response_model", None) if isinstance(route, APIRoute) else None
        candidates = [model, *typing.get_args(model)] if model is not None else []
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                bad += [f"{route.path}:{f}" for f in _model_fields(candidate, set()) if FORBIDDEN_FIELD.search(f)]
    return bad


def test_no_route_serializes_ciphertext_indexes_or_secrets() -> None:
    assert forbidden_response_fields(create_app(Settings(environment="test", db_ssl="disable"))) == []


def test_the_serializer_lint_catches_a_model() -> None:
    from fastapi import FastAPI

    class Inner(BaseModel):
        secret_enc: bytes

    class Out(BaseModel):
        traveler_id: str
        email_bidx: bytes | None = None
        inner: Inner

    app = FastAPI()

    @app.get("/x", response_model=Out)
    def x() -> Out:  # pragma: no cover
        raise NotImplementedError

    assert forbidden_response_fields(app) == ["/x:email_bidx", "/x:secret_enc"]


def test_routers_never_name_secret_columns() -> None:
    for path in sorted((APP / "routers").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "secret_enc" not in text and "wrapped_dek" not in text, path
