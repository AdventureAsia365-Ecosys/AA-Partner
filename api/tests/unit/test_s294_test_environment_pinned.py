"""S294: the test process runs as ENVIRONMENT=test whatever shell started pytest (tests/conftest.py).

Without the pin a bare ``pytest tests/unit`` (no ``ENVIRONMENT`` exported, or ``ENVIRONMENT=dev`` left in the shell)
started the platform settings refresher inside every app a test built, and the deploy gate tests saw a settings read on
connections that must stay untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import platform_settings as ps
from tests import conftest as tests_conftest

API = Path(__file__).resolve().parents[2]


def test_the_process_environment_is_test() -> None:
    assert tests_conftest.TEST_ENVIRONMENT == "test"
    assert os.environ["ENVIRONMENT"] == "test"


@pytest.mark.asyncio
async def test_an_app_lifespan_starts_no_settings_refresher() -> None:
    task = ps.start_refresher(lambda: None)
    await ps.stop_refresher(task)
    assert task is None


@pytest.mark.parametrize("shell_value", [None, "dev"])
def test_the_pin_holds_whatever_the_shell_exported(shell_value: str | None) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    if shell_value is not None:
        env["ENVIRONMENT"] = shell_value
    probe = (
        "import os, tests.conftest  # noqa: F401\n"  # what pytest does first: import the root conftest
        "print(os.environ['ENVIRONMENT'])\n"
    )
    out = subprocess.run(  # noqa: S603 (this interpreter, a fixed probe)
        [sys.executable, "-c", probe], cwd=API, env=env, capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == "test"
