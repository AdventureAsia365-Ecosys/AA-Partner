"""AA-1032 — CI integration shards: the split never drops or doubles a test file, the required check keeps its name.

- ``.github/scripts/ci_shards.py`` (a byte copy of AA-CIS-App's, tested in depth there): on this repo's integration
  suite, every file lands in exactly one shard and the consecutive ranges concatenate to the unsharded order.
- ``.github/workflows/ci.yml``: "Integration Tests" is the aggregate of the "Integration shard i/N" matrix, runs when a
  shard failed (never "skipped", which a required check counts as passed) and fails unless every shard passed; the
  matrix size equals the shard count each shard splits by; skips still fail a shard; Unit Tests run on 4 workers.
- ``.github/test-durations/integration.json``: its keys are real test files under the names ``discover`` returns
  (pytest's rootdir is ``api/``, so junit paths need no ``--prefix``; a refresh under other names would turn the split
  into a file count).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
API = REPO / "api"
SCRIPT = REPO / ".github" / "scripts" / "ci_shards.py"
DURATIONS = REPO / ".github" / "test-durations" / "integration.json"
CI = REPO / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("ci_shards", SCRIPT)
assert _spec is not None and _spec.loader is not None
shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shards)


def _job(name: str) -> str:
    text = CI.read_text(encoding="utf-8")
    body = text.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z][a-z-]*:\n", body, maxsplit=1)[0]


def test_the_integration_suite_splits_into_consecutive_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(API)
    files = shards.discover(["tests/integration"], [])
    assert "tests/integration/test_trip_cancel_api.py" in files
    known = shards.load_durations(str(DURATIONS))
    assert known and all(k.startswith("tests/integration/test_") for k in known)
    for n in (2, 3, 4):
        buckets = shards.plan_contiguous(files, known, n)
        shards.check_partition(files, buckets)
        assert all(buckets) and [f for b in buckets for f in b] == files


def test_the_cli_shards_cover_the_suite_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    for i in (1, 2, 3):
        out = subprocess.run(  # noqa: S603 (our own script, fixed arguments)
            [sys.executable, str(SCRIPT), "files", "--contiguous", "--shards", "3", "--index", str(i),
             "--durations", str(DURATIONS), "tests/integration"],
            cwd=API, capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert out.returncode == 0, out.stderr
        seen += out.stdout.split()
    monkeypatch.chdir(API)
    assert seen == shards.discover(["tests/integration"], [])  # every file once, in the unsharded order


def test_junit_durations_read_the_xunit1_file_attribute(tmp_path: Path) -> None:
    report = tmp_path / "junit-1.xml"
    report.write_text(
        '<testsuites><testsuite><testcase file="tests/integration/test_a.py" classname="x" name="t" time="1.5"/>'
        '<testcase file="tests/integration/test_a.py" classname="x" name="u" time="0.5"/></testsuite></testsuites>'
    )
    out_file = tmp_path / "d.json"
    out = subprocess.run(  # noqa: S603 (our own script, fixed arguments)
        [sys.executable, str(SCRIPT), "durations", "--out", str(out_file), str(report)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert json.loads(out_file.read_text()) == {"tests/integration/test_a.py": 2.0}


def test_committed_durations_name_the_files_discover_returns() -> None:
    # No ini file under tests/integration: pytest's rootdir stays api/, so junit "file" paths match discover() as is.
    # (CIS has tests/integration/pytest.ini and passes durations --prefix tests/integration/.)
    assert not any((API / "tests" / "integration" / n).exists() for n in ("pytest.ini", "pyproject.toml", "tox.ini"))
    keys = list(shards.load_durations(str(DURATIONS)))
    real = [k for k in keys if (API / k).is_file()]
    # A few stale keys (deleted tests) are harmless; a refresh written under other names matches almost none.
    assert keys and len(real) >= 0.8 * len(keys), f"{len(real)}/{len(keys)} keys are test files"


def test_integration_tests_is_the_aggregate_of_its_shards() -> None:
    agg, shard = _job("test-integration"), _job("test-integration-shard")
    assert "name: Integration Tests\n" in agg
    assert "needs: test-integration-shard\n" in agg and "if: ${{ !cancelled() }}\n" in agg
    assert "RESULT: ${{ needs.test-integration-shard.result }}" in agg
    assert '[ "$RESULT" != "success" ]' in agg and "exit 1" in agg
    count = int(re.search(r"INTEGRATION_SHARDS: (\d+)", shard).group(1))  # type: ignore[union-attr]
    assert f"shard: [{', '.join(str(i) for i in range(1, count + 1))}]" in shard
    assert "fail-fast: false" in shard
    assert f"name: Integration shard ${{{{ matrix.shard }}}}/{count}\n" in shard
    assert 'files --contiguous --shards "$INTEGRATION_SHARDS"' in shard and '--index "$SHARD"' in shard
    # A skipped integration test still fails the shard; every shard has its own database.
    assert 'grep -qE "^SKIPPED|[0-9]+ skipped"' in shard and "postgres:15" in shard
    assert "Integration Tests" not in shard


def test_unit_tests_run_on_four_workers_with_the_same_coverage_gate() -> None:
    unit = _job("test-unit")
    assert "-n 4 --dist loadfile" in unit and "--cov=app" in unit and "--cov-fail-under=90" in unit
    pins = (API / "requirements-dev.txt").read_text()
    assert re.search(r"^pytest-xdist==\d+\.\d+\.\d+$", pins, re.M)
