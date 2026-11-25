"""scripts/check_map_engine_provenance.py: the vendored map engine (web/lib/map-engine) is what AA-TripPlanner-Web has
merged, at the lock's ref on main or on main today (AA-948, card CRM-20; the root ADR 0016 byte copy pattern)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_map_engine_provenance.py"
sys.path.insert(0, str(SCRIPT.parent))
_spec = importlib.util.spec_from_file_location("check_map_engine_provenance", SCRIPT)
assert _spec and _spec.loader
prov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prov)

REF = "b" * 40
MANIFEST = b'{"canonical": {}, "files": {"geo.ts": "x"}}\n'
OTHER = b'{"canonical": {}, "files": {"geo.ts": "y"}}\n'


def _repo(tmp_path: Path, manifest: bytes = MANIFEST, **lock_overrides: object) -> Path:
    copy = tmp_path / "web" / "lib" / "map-engine"
    copy.mkdir(parents=True)
    (copy / "MANIFEST.json").write_bytes(manifest)
    lock = {
        "canonical": {"repo": prov.CANONICAL_REPO, "path": prov.CANONICAL_PATH},
        "ref": REF,
        "dest": "web/lib/map-engine",
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    lock.update(lock_overrides)
    (tmp_path / "web" / "map-engine.lock.json").write_text(json.dumps(lock))
    return tmp_path / "web" / "map-engine.lock.json"


def _github(at_ref: bytes | None, on_main: bytes | None, status: str | None = "ahead"):
    calls: list[str] = []

    def get(path: str, accept: str) -> bytes | None:
        calls.append(path)
        if path.endswith(f"?ref={REF}"):
            return at_ref
        if path.endswith("?ref=main"):
            return on_main
        if "/compare/" in path:
            return None if status is None else json.dumps({"status": status}).encode()
        raise AssertionError(path)

    return get, calls


def test_a_copy_of_a_merged_ref_passes(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    get, calls = _github(MANIFEST, OTHER)
    problems, note = prov.check_lock(lock, get, tmp_path)
    assert problems == [] and "on main" in note
    assert any(prov.CANONICAL_PATH in c for c in calls)


def test_a_squash_merged_branch_ref_passes_when_main_has_the_same_manifest(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    get, _ = _github(MANIFEST, MANIFEST, status="diverged")
    problems, note = prov.check_lock(lock, get, tmp_path)
    assert problems == [] and "not on main" in note


def test_an_unmerged_or_edited_copy_fails(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    get, _ = _github(MANIFEST, OTHER, status="diverged")  # the TripPlanner change is not merged yet
    problems, _ = prov.check_lock(lock, get, tmp_path)
    assert problems and "merge the engine change in AA-TripPlanner-Web first" in problems[0]
    get, _ = _github(OTHER, OTHER)  # an edited copy with a regenerated manifest
    assert prov.check_lock(lock, get, tmp_path)[0]


def test_the_lock_must_name_the_canonical_source_a_full_ref_and_the_copied_manifest(tmp_path: Path) -> None:
    get, _ = _github(MANIFEST, MANIFEST)
    wrong = _repo(tmp_path / "a", canonical={"repo": "x"})
    assert "canonical must be" in prov.check_lock(wrong, get, tmp_path / "a")[0][0]
    assert "full 40-character" in prov.check_lock(_repo(tmp_path / "b", ref="main"), get, tmp_path / "b")[0][0]
    assert "repo-relative" in prov.check_lock(_repo(tmp_path / "c", dest="../x"), get, tmp_path / "c")[0][0]
    bad = _repo(tmp_path / "d", manifest_sha256="0" * 64)
    assert "manifest_sha256" in prov.check_lock(bad, get, tmp_path / "d")[0][0]


def test_github_unreachable_is_a_failure(tmp_path: Path) -> None:
    lock = _repo(tmp_path)

    def down(path: str, accept: str) -> bytes | None:
        raise prov.FetchError("could not read")

    assert "cannot verify" in prov.check_lock(lock, down, tmp_path)[0][0]


def test_main_skips_github_when_the_copy_did_not_change(tmp_path: Path) -> None:
    lock = _repo(tmp_path)

    def git(args: Sequence[str]) -> str | None:
        return "" if args[0] == "diff" else "ok"

    def never(path: str, accept: str) -> bytes | None:
        raise AssertionError("GitHub must not be read")

    out = io.StringIO()
    code = prov.main([str(lock), "--base", "c" * 40], get=never, git=git, out=out, root=tmp_path)
    assert code == 0 and "not re-checked" in out.getvalue()
    get, _ = _github(MANIFEST, OTHER, status=None)
    out = io.StringIO()
    assert prov.main([str(lock)], get=get, out=out, root=tmp_path) == 1
    assert "FAIL" in out.getvalue()
