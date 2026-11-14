"""AA-1096 l5 (crm-068) — serve chosen CRM setting values in a test, as an approved platform row would.

crm-068 retired the env overrides of the seven settings below, and with them the ``Settings`` fields a test app used
to pass them through (``Settings(rate_limit_intake_per_minute=3)`` -> ``settings_env()`` -> the reader). Tests now
serve the value itself:

    from tests import settings_serve
    settings_serve.serve({ps.RATE_INTAKE: 3})

``LocalPools.settings(**overrides)`` (tests/tokens.py) still takes the old field names and serves them through
:func:`take_fields`, so a test written for the field keeps reading the same way. The served values live until the
next test file (tests/unit/conftest.py resets them per module, as it reset the env overrides before). No DB.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app import platform_settings as ps
from app.platform_settings.reader import Resolution, coerce, parse_text

# The old Settings field of each retired override -> its setting.
FIELD_KEYS: dict[str, str] = {
    "ai_budget_max_usd": ps.AI_BUDGET_MAX,
    "crm_llm_max_tokens": ps.AI_DRAFT_MAX_TOKENS,
    "rate_limit_public_per_minute": ps.RATE_PUBLIC,
    "rate_limit_intake_per_minute": ps.RATE_INTAKE,
    "rate_limit_intake_form_per_hour": ps.RATE_INTAKE_FORM,
    "rate_limit_member_per_minute": ps.RATE_MEMBER,
    "rate_limit_ai_per_minute": ps.RATE_AI,
}
SERVED: dict[str, Any] = {}
_REAL = ps.READER.resolve


def _resolve(key: str, tenant_id: Any = None) -> Resolution:
    if key in SERVED:
        return Resolution(key, SERVED[key], "platform", version=1)
    return _REAL(key, tenant_id)


def serve(values: Mapping[str, Any]) -> None:
    """Serve ``{setting key: value}`` (parsed as the reader parses a stored text value; a bad value raises)."""
    for key, raw in values.items():
        d = ps.READER.definition(key)
        value, problem = parse_text(d, str(raw))
        if problem is not None:
            raise ValueError(f"{key}: {raw!r}: {problem}")
        SERVED[key] = coerce(d, value)
    ps.READER.resolve = _resolve  # type: ignore[method-assign]


def reset() -> None:
    SERVED.clear()
    ps.READER.resolve = _REAL  # type: ignore[method-assign]


def take_fields(overrides: dict[str, Any]) -> dict[str, Any]:
    """Pop the retired field names out of ``overrides`` and serve them; returns the rest."""
    served = {FIELD_KEYS[name]: overrides.pop(name) for name in list(overrides) if name in FIELD_KEYS}
    if served:
        serve(served)
    return overrides


def lay_retired_overrides(monkeypatch: Any) -> None:
    """Give the seven keys their retired env names back as ``env_override`` (for the length of one test).

    For the tests of the env-override mechanism itself (the reader's step 1, the 409 ``env_override_active`` refusal,
    the "overridden by env" source), which stays for a future one-release override although no definition carries one
    since crm-068. Snapshot: ``tests/unit/snapshots/aa1096_retired_env_overrides.json``."""
    import dataclasses
    import json
    from pathlib import Path

    retired = json.loads(
        (Path(__file__).resolve().parent / "unit" / "snapshots" / "aa1096_retired_env_overrides.json").read_text(
            encoding="utf-8"
        )
    )
    for key, env in retired.items():
        monkeypatch.setitem(ps.READER._defs, key, dataclasses.replace(ps.READER.definition(key), env_override=env))
