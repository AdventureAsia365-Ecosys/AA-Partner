# AA-1024 — unit suite "order flakes" (S294, lane l19-partner-order-flakes)

Report: a lane ran the whole unit suite with `pytest tests/unit -n 2 --dist loadfile` and coverage and saw 8 failures
(tests/unit/test_deploy_gate.py: 7, tests/unit/test_aa1096_platform_settings.py: the refresher test) that passed when
the two files ran alone.

## Decisions

- **Root cause is the process environment, not the test order.** On origin/main the suite is green with
  `ENVIRONMENT=test` (what ci.yml and scripts/pre-push.sh export) and fails the exact same 8 tests whenever
  `ENVIRONMENT` is missing, whatever the worker count or order (the two files alone fail too). Without it
  `app.platform_settings.start_refresher` (it reads `os.environ`, not the app's `Settings`) starts the background
  settings refresher in every `TestClient(create_app(...))` lifespan: the deploy gate tests then see a
  `crm.setting_value` read on connections that must stay untouched (`conn.calls == []`), and the refresher test's first
  check (`start_refresher(...) is None`) fails.
- **Pin it at the source of the test run**: a new `api/tests/conftest.py` sets `ENVIRONMENT=test` at import, before any
  test module imports the app, for unit and integration tests. Forced, not `setdefault`: a shell that exported
  `ENVIRONMENT=dev` broke the suite the same way. A test that needs another value already uses `monkeypatch.setenv`.
- **No app change.** In a deployed task `Settings.environment` and the process env are the same value, so the
  refresher reading `os.environ` is not a live bug; passing the app's `Settings.environment` to `start_refresher` would
  be a cleaner seam but is not needed for this fix.

## Changed

- `api/tests/conftest.py` (new): the pin.
- `api/tests/unit/test_aa1096_platform_settings.py`: the refresher test stops the task of its first call before
  asserting, so a failing check never leaves a running refresher on the loop.
- `api/tests/unit/test_s294_test_environment_pinned.py` (new): guard — the process runs as `test`, a lifespan starts no
  refresher, and importing the root conftest under a shell with `ENVIRONMENT` unset or `dev` still yields `test`.

## Tradeoffs

- Pinning in a conftest rather than in `pyproject.toml` (`[tool.pytest.ini_options] env`) — that needs the
  pytest-env plugin, a new dependency for one variable.

## Should know

- Reproduction on origin/main: with `ENVIRONMENT=test` the whole suite is green at `-n 2` (5336 passed); without it
  the two files fail the same 8 tests even when they run alone, so worker count and order never mattered.
- Proof (origin/main + this change, shell WITHOUT `ENVIRONMENT`, `--dist loadfile`, coverage, gate 90, each run a
  different seeded shuffle of the 233 unit files): `-n 2` seeds 11/22/33 and `-n 4` seeds 44/55/66 — six runs, each
  5340 passed, 1 skipped, TOTAL 93%. The two files alone pass with `ENVIRONMENT` unset, `dev` and `test`.
