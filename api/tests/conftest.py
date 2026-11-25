"""Process environment every test run starts from, whatever shell started pytest.

S294: every CI step and scripts/pre-push.sh run pytest with ``ENVIRONMENT=test``, so tests came to rely on it (the
platform settings refresher stays off, ops alerts tag ``test``). A bare ``pytest tests/unit`` from a shell without it
(an agent lane, a developer) ran the app's background refresher against the scripted connections and failed
tests/unit/test_deploy_gate.py and the refresher test in tests/unit/test_aa1096_platform_settings.py, which looked like
a test-order flake. Pinned here, at conftest import (before any test module imports the app), for unit and
integration tests alike; a test that needs another value sets it with ``monkeypatch.setenv``.
"""

import os as _os

TEST_ENVIRONMENT = "test"

_os.environ["ENVIRONMENT"] = TEST_ENVIRONMENT
