"""Provenance of the vendored map engine (AA-948, card CRM-20; the root ADR 0016 guarded byte copy pattern).

``web/lib/map-engine/`` is a byte copy of AA-TripPlanner-Web ``frontend/lib/map-engine/`` (the shared map engine,
extracted from the planner map), written by that folder's ``check-map-engine.mjs --sync``, which records the
TripPlanner commit in ``web/map-engine.lock.json`` (``ref``) with the sha256 of the copied ``MANIFEST.json``. The
integrity check (``check-map-engine.mjs``, offline, in Web Build and the unit tests) proves that the copy matches its
own manifest. This check proves where that manifest came from:

1. the lock names the canonical source (``AdventureAsia365-Ecosys/AA-TripPlanner-Web``, ``frontend/lib/map-engine``);
2. the copied ``MANIFEST.json`` has the lock's ``manifest_sha256``;
3. it is byte-identical to the canonical ``MANIFEST.json`` at ``ref`` and ``ref`` is on AA-TripPlanner-Web ``main``,
   **or** it is byte-identical to the canonical ``MANIFEST.json`` on ``main`` today. The second form lets the engine
   change and its first consumer ship in one wave: the copy is synced from the TripPlanner branch, whose commit is not
   on ``main`` once the TripPlanner pull request is squash-merged; the copy is still exactly what TripPlanner merged.
   A later sync refreshes ``ref``.

An edited copy with a regenerated manifest fails (3); a copy of an unmerged TripPlanner change fails until TripPlanner
merges it. Like the kit check it only reads GitHub when the change touches the lock or the copy (``--base``).

Usage (any working directory; lock paths are relative to it)::

    python3 api/scripts/check_map_engine_provenance.py web/map-engine.lock.json [--base <sha|ref>]

Standard library only (plus the kit check's GitHub helpers next to this file). Exit codes: 0 fine, 1 a lock failed,
2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_kit_provenance import (  # noqa: E402 — the sibling script, imported by path
    REPO_ROOT,
    SHA_RE,
    FetchError,
    Getter,
    GitRunner,
    github_get,
    kit_changed,
    run_git,
)

CANONICAL_REPO = "AdventureAsia365-Ecosys/AA-TripPlanner-Web"
CANONICAL_PATH = "frontend/lib/map-engine"
MAIN = "main"
FIX = (
    "re-sync with AA-TripPlanner-Web frontend/lib/map-engine/check-map-engine.mjs --sync from its main (never edit "
    "web/lib/map-engine, its MANIFEST.json or web/map-engine.lock.json by hand)"
)


def _lock_problem(lock: object) -> str | None:
    """What is wrong with the lock's own fields (None when the canonical source, ref and dest are well formed)."""
    if not isinstance(lock, dict):
        return "the lock is not a JSON object"
    expected = {"repo": CANONICAL_REPO, "path": CANONICAL_PATH}
    canonical = lock.get("canonical") or {}
    if not isinstance(canonical, dict) or {k: canonical.get(k) for k in expected} != expected:
        return f"canonical must be {{repo: {CANONICAL_REPO}, path: {CANONICAL_PATH}}}, got {canonical!r}; {FIX}"
    ref = lock.get("ref")
    if not isinstance(ref, str) or not SHA_RE.match(ref):
        return f"ref must be a full 40-character commit sha of AA-TripPlanner-Web, got {ref!r}; {FIX}"
    dest = lock.get("dest")
    if not isinstance(dest, str) or not dest or dest.startswith("/") or ".." in Path(dest).parts:
        return f"dest must be a repo-relative folder, got {dest!r}; {FIX}"
    return None


def _upstream_note(local: bytes, ref: str, get: Getter) -> str | None:
    """How the copied manifest matches AA-TripPlanner-Web (at a merged ref, or on main today); None = it does not."""
    repo = f"/repos/{CANONICAL_REPO}"
    raw = "application/vnd.github.raw"
    if get(f"{repo}/contents/{CANONICAL_PATH}/MANIFEST.json?ref={ref}", raw) == local:
        compare = get(f"{repo}/compare/{urllib.parse.quote(ref)}...{MAIN}", "application/vnd.github+json")
        if compare is not None and json.loads(compare).get("status") in ("ahead", "identical"):
            return f"= {CANONICAL_REPO}@{ref[:12]} (on {MAIN})"
    if get(f"{repo}/contents/{CANONICAL_PATH}/MANIFEST.json?ref={MAIN}", raw) == local:
        return f"= {CANONICAL_REPO} {MAIN} (lock ref {ref[:12]} is not on {MAIN}; the next sync refreshes it)"
    return None


def check_lock(lock_path: Path, get: Getter, root: Path = REPO_ROOT) -> tuple[list[str], str]:
    """(the provenance problems of one map-engine.lock.json, a note on how it passed)."""
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cannot read the lock: {exc}"], ""
    problem = _lock_problem(lock)
    if problem:
        return [problem], ""
    dest = lock["dest"]
    try:
        local = (root / dest / "MANIFEST.json").read_bytes()
    except OSError:
        return [f"{dest}/MANIFEST.json is missing; {FIX}"], ""
    if hashlib.sha256(local).hexdigest() != lock.get("manifest_sha256"):
        return [f"{dest}/MANIFEST.json does not have the lock's manifest_sha256; {FIX}"], ""
    try:
        note = _upstream_note(local, lock["ref"], get)
    except FetchError as exc:
        return [f"cannot verify against AA-TripPlanner-Web: {exc}"], ""
    except ValueError as exc:
        return [f"unexpected GitHub compare answer: {exc}"], ""
    if note:
        return [], note
    return [
        f"{dest}/MANIFEST.json is neither AA-TripPlanner-Web's at a merged ref nor its {MAIN} today: merge the engine "
        f"change in AA-TripPlanner-Web first, then sync; {FIX}"
    ], ""


def main(
    argv: Sequence[str] | None = None,
    get: Getter = github_get,
    git: GitRunner = run_git,
    out: TextIO = sys.stdout,
    root: Path = REPO_ROOT,
) -> int:
    parser = argparse.ArgumentParser(description="Provenance of the vendored map engine (AA-948).")
    parser.add_argument("locks", nargs="+", type=Path, help="map-engine.lock.json files")
    parser.add_argument("--base", default="", help="diff base (sha or ref); unchanged copy = pass without GitHub")
    args = parser.parse_args(argv)
    failed = 0
    for lock_path in args.locks:
        rel = os.path.relpath(lock_path.resolve(), root.resolve())
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            dest = lock.get("dest") if isinstance(lock, dict) else None
        except (OSError, ValueError):
            dest = None
        watched = [rel] + ([dest] if isinstance(dest, str) and dest else [])
        if args.base and not kit_changed(watched, args.base, git):
            print(f"ok    {rel}: map engine copy unchanged since {args.base[:12]}; not re-checked", file=out)
            continue
        problems, note = check_lock(lock_path, get, root)
        if problems:
            failed += 1
            for problem in problems:
                print(f"FAIL  {rel}: {problem}", file=out)
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print(f"::error file={rel}::{problem}", file=out)
        else:
            print(f"ok    {rel}: MANIFEST.json {note}", file=out)
    print(f"{len(args.locks)} lock(s) checked, {failed} failed", file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
