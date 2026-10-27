"""scripts/check_vendored_copies.py: the drift check of the files vendored from AA-CIS-App (job table template, AA-751;
multi-issuer verifier, AA-680)."""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_vendored_copies.py"
_spec = importlib.util.spec_from_file_location("check_vendored_copies", SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _local(tmp_path: Path) -> Path:
    for name in check.COPIES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(f"-- {name}\nline two\n".encode())
    return tmp_path


def test_the_vendored_files_exist_and_are_listed() -> None:
    assert all((check.API_DIR / name).is_file() for name in check.COPIES)
    assert check.COPIES["app/auth/identity_verifier.py"] == "api/core/identity_verifier.py"


def test_byte_copies_pass(tmp_path: Path) -> None:
    local = _local(tmp_path)
    by_source = {src: (local / name).read_bytes() for name, src in check.COPIES.items()}
    out = io.StringIO()
    assert check.run(local, by_source.get, out) == 0
    assert "3 file(s) checked, 0 failed" in out.getvalue()


def test_a_one_byte_difference_fails_with_its_position(tmp_path: Path) -> None:
    local = _local(tmp_path)
    by_source = {src: (local / name).read_bytes() for name, src in check.COPIES.items()}
    by_source["shared/jobs/job_table_shape.py"] = by_source["shared/jobs/job_table_shape.py"].replace(b"two", b"twO")
    out = io.StringIO()
    assert check.run(local, by_source.get, out) == 1
    assert "differs from AdventureAsia365-Ecosys/AA-CIS-App@main:shared/jobs/job_table_shape.py" in out.getvalue()
    assert "(line 2)" in out.getvalue()


def test_missing_upstream_missing_copy_and_fetch_errors_fail(tmp_path: Path) -> None:
    local = _local(tmp_path)
    (local / "tests/job_template/job_table_shape.py").unlink()

    def fetch(path: str) -> bytes | None:
        raise check.FetchError("HTTP 500")

    assert "missing; copy" in check.check_copy("tests/job_template/job_table_shape.py", local, fetch)
    assert "cannot compare" in check.check_copy("tests/job_template/job_table_template.sql", local, fetch)
    assert "not found on" in check.check_copy("app/auth/identity_verifier.py", local, lambda _p: None)
    assert "prefix" in check.first_difference(b"ab", b"abc")


def test_github_fetch_retries_then_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(req, timeout: float):
        calls.append(req.full_url)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(check.urllib.request, "urlopen", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(check.FetchError, match="network error"):
        check.github_fetch("shared/jobs/job_table_shape.py", attempts=2, sleep=lambda _s: None)
    assert len(calls) == 2 and calls[0].endswith("/contents/shared/jobs/job_table_shape.py?ref=main")


def test_web_lib_copies_exist_and_main_checks_every_set() -> None:
    assert all((check.WEB_DIR / name).is_file() for name in check.WEB_COPIES)
    assert check.WEB_COPIES["lib/bffPath.ts"] == "frontend/lib/bffPath.ts"
    by_source = {src: (check.API_DIR / name).read_bytes() for name, src in check.COPIES.items()}
    by_source |= {src: (check.PORTAL_DIR / name).read_bytes() for name, src in check.PORTAL_COPIES.items()}
    by_source |= {src: (check.WEB_DIR / name).read_bytes() for name, src in check.WEB_COPIES.items()}
    out = io.StringIO()
    assert check.main(by_source.get, out) == 0
    assert f"{len(check.WEB_COPIES)} file(s) checked, 0 failed" in out.getvalue()
    assert "ok    web/lib/bffPath.ts" in out.getvalue()
    by_source["frontend/lib/bffPath.ts"] = b"drifted"
    out = io.StringIO()
    assert check.main(by_source.get, out) == 1
    assert "FAIL  web/lib/bffPath.ts" in out.getvalue()


def test_the_ui_kit_is_left_to_the_kit_lock_checks() -> None:
    """AA-994 S2a (root ADR 0016 decision 5): no kit or brand file is live-compared with CIS main any more; the kit
    core copies are pinned by kit.lock.json and checked by check-kit-copy.mjs + check_kit_provenance.py instead."""
    for copies in (check.WEB_COPIES, check.PORTAL_COPIES):
        assert all(name.startswith("lib/") for name in copies), copies
        assert not any("_kit" in src or "_brand" in src for src in copies.values())
    assert set(check.PORTAL_COPIES) == set(check.WEB_COPIES)
    for app in (check.PORTAL_DIR, check.WEB_DIR):
        assert (app / "kit.lock.json").is_file()
        assert (app / "app" / "_kit" / "core" / "MANIFEST.json").is_file()
        assert not (app / "app" / "_brand").exists()
