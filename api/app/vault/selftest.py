"""Live check of the key provider without touching tenant data: ``python -m app.vault.selftest``.

Runs inside the API container (ECS Exec on Dev, the task role's KMS grant): builds the provider from the process
settings, generates one throwaway DEK under the reserved nil tenant context ``{tenant_id: 0000…, purpose: vault}``
through the active wrapper and unwraps it again. Nothing is stored. Prints one JSON line and exits 0 on success,
1 otherwise (codes only: never a key, a blob or a KMS message).
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.settings import load_settings
from app.vault.errors import KeyUnavailable, UnwrapDenied
from app.vault.provider import build_key_provider


async def run() -> dict[str, object]:
    settings = load_settings()
    keys = build_key_provider(settings)
    if keys is None:
        return {"ok": False, "keys": settings.keys_state, "error": "key_provider_not_configured"}
    try:
        await keys.wrapper(keys.active_provider).self_test()
    except (KeyUnavailable, UnwrapDenied) as exc:
        code = exc.reason if isinstance(exc, KeyUnavailable) else str(exc)
        return {"ok": False, "keys": settings.keys_state, "error": code}
    return {"ok": True, "keys": settings.keys_state}


def main() -> int:
    result = asyncio.run(run())
    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
