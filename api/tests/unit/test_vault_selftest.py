"""``python -m app.vault.selftest``: the Dev live check of the key provider (JSON verdict, exit code)."""

from __future__ import annotations

import json

import pytest

from app.vault import selftest
from app.vault.errors import KeyUnavailable


def test_local_provider_passes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("KEY_PROVIDER", "local")
    assert selftest.main() == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "keys": "local"}


def test_missing_kms_key_is_reported(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("KEY_PROVIDER", raising=False)
    monkeypatch.delenv("KMS_DATA_KEY_ID", raising=False)
    assert selftest.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "keys": "not_configured",
        "error": "key_provider_not_configured",
    }


def test_kms_failure_is_a_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("KMS_DATA_KEY_ID", "alias/crm-dev/data")

    async def refuse(self) -> None:  # noqa: ANN001
        raise KeyUnavailable("AccessDeniedException")

    monkeypatch.setattr("app.vault.wrapping.KmsKeyWrapper.self_test", refuse)
    assert selftest.main() == 1
    assert json.loads(capsys.readouterr().out)["error"] == "AccessDeniedException"
