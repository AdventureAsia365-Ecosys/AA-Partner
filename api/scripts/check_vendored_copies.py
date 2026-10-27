"""Checks that the files AA-Partner vendors from AA-CIS-App are byte copies of AA-CIS-App main.

- ``tests/job_template/job_table_template.sql`` and ``job_table_shape.py`` = AA-CIS-App ``shared/jobs/`` (AA-751, X6;
  ADR 0003a decision 2: one DDL template, owned by CIS; every owner's CI compares its job table with it);
- ``app/auth/identity_verifier.py`` = AA-CIS-App ``api/core/identity_verifier.py`` (AA-680 Phase 2; root ADR 0006 §8
  rule 9: the multi-issuer verifier is copied byte-identical into each app repo);
- the CRM web's BFF helpers (``web/``, AA-935): ``web/lib/{sameOrigin,crossSiteGuard,bffPath,proxyResponse}.ts`` =
  AA-CIS-App ``frontend/lib/*`` (the AA-911 cross-site rule and the AA-577 path rule of every BFF);
- the tenant portal's (``portal/``, AA-979 CRM-P2a, moved from AA-CIS-App with AA-885's vendored list):
  ``portal/lib/{sameOrigin,crossSiteGuard,bffPath,proxyResponse}.ts`` = the same AA-CIS-App ``frontend/lib`` files.

The UI kit is not checked here any more (AA-994 S2a, root ADR 0016 decision 5): ``portal/app/_kit/core/`` and
``web/app/_kit/core/`` are synced by the root ``tools/kit/kit-sync.sh`` and pinned to a CIS commit by
``kit.lock.json``; Portal Build and Web Build check them offline (``core/check-kit-copy.mjs``, integrity) and against
that commit (``scripts/check_kit_provenance.py``, provenance). A live compare with CIS main turned every Partner PR red
whenever CIS merged a kit change, which is why this job could never be required.

This check fails when a copy differs from the file on AA-CIS-App ``main`` by even one byte, or is not on main yet.
Modelled on AA-CIS-Infra ``db/check_migration_copies.py``. CI job "Vendored CIS copies" (non-required).

Usage (from ``api/``)::

    python3 scripts/check_vendored_copies.py

Reads the CIS files through the GitHub contents API (the app repos are public). ``GITHUB_TOKEN`` (or ``GH_TOKEN``) is
used when set (higher rate limit); without it, or if GitHub rejects it, the call is anonymous. Rate limits, network
errors and 5xx are retried, then reported as a FAIL (never a traceback). Standard library only: CI needs no install.
"""

from __future__ import annotations

import hashlib
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
API_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = API_DIR.parent / "web"
PORTAL_DIR = API_DIR.parent / "portal"
# path under api/ -> path in AA-CIS-App
COPIES: dict[str, str] = {
    "tests/job_template/job_table_template.sql": "shared/jobs/job_table_template.sql",
    "tests/job_template/job_table_shape.py": "shared/jobs/job_table_shape.py",
    "app/auth/identity_verifier.py": "api/core/identity_verifier.py",
}
_WEB_LIB = ("sameOrigin.ts", "crossSiteGuard.ts", "bffPath.ts", "proxyResponse.ts")
# path under web/ -> path in AA-CIS-App (AA-935). The UI kit is checked by kit.lock.json since AA-994 S2a.
WEB_COPIES: dict[str, str] = {f"lib/{name}": f"frontend/lib/{name}" for name in _WEB_LIB}
# path under portal/ -> path in AA-CIS-App (AA-979). The UI kit is checked by kit.lock.json since AA-994 S2a.
PORTAL_COPIES: dict[str, str] = {f"lib/{name}": f"frontend/lib/{name}" for name in _WEB_LIB}


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


def github_fetch(path: str, attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> bytes | None:
    url = f"https://api.github.com/repos/{ORG}/{REPO}/contents/{path}?ref={BRANCH}"
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
                    raise FetchError(f"GitHub API {last} for {REPO}:{path}") from None
                break  # 5xx: back off and retry
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"network error {exc}"
                break
        if attempt + 1 < attempts:
            sleep(2 * (attempt + 1))
    hint = "" if token else " (anonymous: 60 requests/hour; set GITHUB_TOKEN or GH_TOKEN)"
    raise FetchError(f"could not read {REPO}:{path} from GitHub: {last}{hint}")


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


def main(fetch: Fetcher = github_fetch, out: TextIO = sys.stdout) -> int:
    """All three sets: the API's copies (under api/), the web's (under web/) and the portal's (under portal/)."""
    api = run(API_DIR, fetch, out)
    web = run(WEB_DIR, fetch, out, WEB_COPIES, "web")
    portal = run(PORTAL_DIR, fetch, out, PORTAL_COPIES, "portal")
    return 1 if (api or web or portal) else 0


if __name__ == "__main__":
    sys.exit(main())
