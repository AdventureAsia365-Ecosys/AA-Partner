"""Checks that the files AA-Partner vendors from AA-CIS-App are byte copies of AA-CIS-App main.

- ``tests/job_template/job_table_template.sql`` and ``job_table_shape.py`` = AA-CIS-App ``shared/jobs/`` (AA-751, X6;
  ADR 0003a decision 2: one DDL template, owned by CIS; every owner's CI compares its job table with it);
- ``app/auth/identity_verifier.py`` = AA-CIS-App ``api/core/identity_verifier.py`` (AA-680 Phase 2; root ADR 0006 §8
  rule 9: the multi-issuer verifier is copied byte-identical into each app repo);
- the CRM web's BFF helpers (``web/``, AA-935): ``web/lib/{sameOrigin,crossSiteGuard,bffPath,
  proxyResponse,upstreamHeaders}.ts`` =
  AA-CIS-App ``frontend/lib/*`` (the AA-911 cross-site rule and the AA-577 path rule of every BFF); the Content
  studio's BFF uses the same copies since it moved into the web (AA-1036; the separate ``portal/`` copies went with it);
- AA-970: ``web/lib/legal/annex3.generated.json`` = the public summary of AA-Ecosys (the root repo, branch ``master``)
  ``docs/legal/dpa-annex-3-subprocessors.md``, as ``scripts/gen_legal_annex.py`` writes it (not a byte copy: the JSON
  must equal what the script makes of the root file today). The agency app's public data and sub-processor pages
  render it.

The UI kit is not checked here any more (AA-994 S2a, root ADR 0016 decision 5): ``web/app/_kit/core/`` is synced by
the root ``tools/kit/kit-sync.sh`` and pinned to a CIS commit by ``kit.lock.json``; Web Build checks it offline
(``core/check-kit-copy.mjs``, integrity) and against that commit (``scripts/check_kit_provenance.py``, provenance). A
live compare with CIS main turned every Partner PR red whenever CIS merged a kit change, which is why this job could
never be required.

This check fails when a copy differs from the file on AA-CIS-App ``main`` by even one byte, or is not on main yet.
Modelled on AA-CIS-Infra ``db/check_migration_copies.py``. CI job "Vendored CIS copies" (non-required).

Usage (from ``api/``)::

    python3 scripts/check_vendored_copies.py

Reads the CIS files through the GitHub contents API (the app repos are public). ``GITHUB_TOKEN`` (or ``GH_TOKEN``) is
used when set (higher rate limit); without it, or if GitHub rejects it, the call is anonymous. Rate limits, network
errors and 5xx are retried, then reported as a FAIL (never a traceback). Standard library only: CI needs no install.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

ORG = "AdventureAsia365-Ecosys"
REPO = "AA-CIS-App"
BRANCH = "main"
# AA-970: the root repo, where the DPA annexes live (public, default branch master).
ROOT_REPO = "AA-Ecosys"
ROOT_BRANCH = "master"
API_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = API_DIR.parent / "web"
# path under api/ -> path in AA-CIS-App
COPIES: dict[str, str] = {
    "tests/job_template/job_table_template.sql": "shared/jobs/job_table_template.sql",
    "tests/job_template/job_table_shape.py": "shared/jobs/job_table_shape.py",
    "app/auth/identity_verifier.py": "api/core/identity_verifier.py",
}
_WEB_LIB = ("sameOrigin.ts", "crossSiteGuard.ts", "bffPath.ts", "proxyResponse.ts", "upstreamHeaders.ts")
# path under web/ -> path in AA-CIS-App (AA-935). The UI kit is checked by kit.lock.json since AA-994 S2a.
WEB_COPIES: dict[str, str] = {f"lib/{name}": f"frontend/lib/{name}" for name in _WEB_LIB}


class FetchError(RuntimeError):
    """The CIS file could not be read (rate limit, network, GitHub outage); not a 404."""


# Returns the file's bytes on AA-CIS-App main, None when it is not there (HTTP 404); raises FetchError otherwise.
Fetcher = Callable[[str], bytes | None]


def first_difference(a: bytes, b: bytes) -> str:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            line = a[:i].count(b"\n") + 1
            return f"first difference at byte {i} (line {line})"
    return f"one file is a prefix of the other ({len(a)} vs {len(b)} bytes)"


def check_copy(name: str, base_dir: Path, fetch: Fetcher, copies: dict[str, str] = COPIES) -> str | None:
    """The problem with one copy, or None when it is a byte copy of CIS main."""
    source = copies[name]
    where = f"{ORG}/{REPO}@{BRANCH}:{source}"
    local_path = base_dir / name
    if not local_path.is_file():
        return f"missing; copy {where} here byte for byte"
    local = local_path.read_bytes()
    try:
        upstream = fetch(source)
    except FetchError as exc:
        return f"cannot compare with {where}: {exc}"
    if upstream is None:
        return f"not found on {where}: merge the CIS change first, then re-run this check"
    if upstream != local:
        return (
            f"differs from {where}: {first_difference(local, upstream)}; "
            f"sha256 copy={hashlib.sha256(local).hexdigest()[:12]} cis={hashlib.sha256(upstream).hexdigest()[:12]}. "
            "Copy the CIS file byte for byte (cp, not an editor); never edit the copy here"
        )
    return None


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def github_fetch(
    path: str,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    repo: str = REPO,
    branch: str = BRANCH,
) -> bytes | None:
    url = f"https://api.github.com/repos/{ORG}/{repo}/contents/{path}?ref={branch}"
    token = _token()
    auths = ([token] if token else []) + [""]
    last = ""
    for attempt in range(attempts):
        for auth in auths:
            headers = {
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "aa-partner-vendored-copy-check",
            }
            if auth:
                headers["Authorization"] = f"Bearer {auth}"
            try:
                req = urllib.request.Request(url, headers=headers)  # noqa: S310 — fixed https URL
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https URL
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last = f"HTTP {exc.code}"
                if exc.code in (401, 403, 429):
                    continue  # token refused or rate limited: try anonymous / next round
                if exc.code < 500:
                    raise FetchError(f"GitHub API {last} for {repo}:{path}") from None
                break  # 5xx: back off and retry
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"network error {exc}"
                break
        if attempt + 1 < attempts:
            sleep(2 * (attempt + 1))
    hint = "" if token else " (anonymous: 60 requests/hour; set GITHUB_TOKEN or GH_TOKEN)"
    raise FetchError(f"could not read {repo}:{path} from GitHub: {last}{hint}")


root_fetch: Fetcher = functools.partial(github_fetch, repo=ROOT_REPO, branch=ROOT_BRANCH)


def _annex_module():  # noqa: ANN202 — the sibling script, loaded by path (scripts/ is not a package)
    spec = importlib.util.spec_from_file_location("gen_legal_annex", Path(__file__).with_name("gen_legal_annex.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("scripts/gen_legal_annex.py is missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_annex(fetch: Fetcher, out: TextIO, local: Path | None = None) -> int:
    """AA-970: the agency app's copy of the annex 3 public summary equals what the root file on master gives."""
    gen = _annex_module()
    local = local or gen.OUT
    source = gen.SOURCE["path"]
    rel = "web/lib/legal/annex3.generated.json"
    where = f"{ORG}/{ROOT_REPO}@{ROOT_BRANCH}:{source}"
    problem = None
    try:
        upstream = fetch(source)
    except FetchError as exc:
        upstream, problem = None, f"cannot compare with {where}: {exc}"
    if problem is None and upstream is None:
        problem = f"not found on {where}: merge the root change first, then re-run this check"
    if problem is None:
        try:
            want = gen.render(gen.extract(upstream.decode("utf-8")))
        except gen.AnnexError as exc:
            want, problem = "", f"the annex on {where} has no usable public summary: {exc}"
        if problem is None:
            have = local.read_text(encoding="utf-8") if local.is_file() else ""
            if have != want:
                problem = (
                    f"differs from the public summary of {where}; run "
                    "python3 scripts/gen_legal_annex.py <AA-Ecosys>/docs/legal/dpa-annex-3-subprocessors.md"
                )
    if problem:
        print(f"FAIL  {rel}: {problem}", file=out)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error file={rel}::{problem}", file=out)
        return 1
    print(f"ok    {rel}", file=out)
    return 0


def run(
    base_dir: Path = API_DIR,
    fetch: Fetcher = github_fetch,
    out: TextIO = sys.stdout,
    copies: dict[str, str] = COPIES,
    prefix: str = "api",
) -> int:
    failed = 0
    for name in copies:
        problem = check_copy(name, base_dir, fetch, copies)
        rel = f"{prefix}/{name}"
        if problem:
            failed += 1
            print(f"FAIL  {rel}: {problem}", file=out)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::error file={rel}::{problem}", file=out)
        else:
            print(f"ok    {rel}", file=out)
    print(f"{len(copies)} file(s) checked, {failed} failed", file=out)
    return 1 if failed else 0


def main(fetch: Fetcher = github_fetch, out: TextIO = sys.stdout, fetch_root: Fetcher = root_fetch) -> int:
    """Both sets: the API's copies (under api/) and the web's (under web/), and the annex 3 public summary (AA-970)."""
    api = run(API_DIR, fetch, out)
    web = run(WEB_DIR, fetch, out, WEB_COPIES, "web")
    annex = check_annex(fetch_root, out)
    return 1 if (api or web or annex) else 0


if __name__ == "__main__":
    sys.exit(main())
