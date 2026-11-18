#!/usr/bin/env python3
"""The CRM web's npm audit gate: fail on any high or critical advisory, unless it is accepted with a reason and an expiry.

`npm audit --audit-level=high` cannot accept one advisory, so an advisory with no patched version (S284:
GHSA-fc5q-39xc-vjpf, picomatch ReDoS, every version affected) blocked every pull request. This gate reads
`npm audit --json` and walks each high / critical package down to the advisories that cause it. An advisory passes only
when `.github/npm-audit-allow.json` lists it:

    {"allow": [{"id": "GHSA-xxxx-xxxx-xxxx", "expires": "YYYY-MM-DD",
                "reason": "why it is safe for now, and what removes it"}]}

An entry needs a non-empty reason and an expiry at most MAX_DAYS_AHEAD days ahead; on the expiry day it stops
counting and the gate turns red again, so no acceptance is permanent (same rule as .trivyignore, AA-879).
Stdlib only.

Usage: npm audit --json | npm_audit_gate.py [allowlist] (default .github/npm-audit-allow.json).
TODAY_OVERRIDE=YYYY-MM-DD for tests.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

MAX_DAYS_AHEAD = 90
FAILING = ("high", "critical")
GHSA = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}")


def load_allow(path: str, today: dt.date) -> tuple[dict[str, dt.date], list[str]]:
    """-> (advisory id -> expiry, errors). Expired entries are left out; a bad entry is an error."""
    if not os.path.exists(path):
        return {}, []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    allowed: dict[str, dt.date] = {}
    errors: list[str] = []
    for i, e in enumerate(data.get("allow", [])):
        where = f"{path} entry {i + 1}"
        aid, reason, exp = str(e.get("id", "")), str(e.get("reason", "")).strip(), e.get("expires")
        if not GHSA.fullmatch(aid):
            errors.append(f"{where}: id must be a GHSA id, got {aid!r}")
            continue
        if not reason:
            errors.append(f"{where} ({aid}): a reason is required")
            continue
        try:
            expires = dt.date.fromisoformat(str(exp))
        except ValueError:
            errors.append(f"{where} ({aid}): expires must be YYYY-MM-DD, got {exp!r}")
            continue
        if (expires - today).days > MAX_DAYS_AHEAD:
            errors.append(f"{where} ({aid}): expires {expires} is more than {MAX_DAYS_AHEAD} days ahead")
            continue
        if expires > today:
            allowed[aid] = expires
    return allowed, errors


def failing_advisories(report: dict) -> dict[str, set[str]]:
    """-> advisory id -> the packages it makes high / critical (walking `via` chains to the advisories)."""
    vulns = report.get("vulnerabilities", {})
    found: dict[str, set[str]] = {}

    def walk(name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in vulns:
            return set()
        seen.add(name)
        ids: set[str] = set()
        for via in vulns[name].get("via", []):
            if isinstance(via, dict):
                if via.get("severity") in FAILING:
                    m = GHSA.search(str(via.get("url", "")))
                    ids.add(m.group(0) if m else str(via.get("url") or via.get("source")))
            else:
                ids |= walk(str(via), seen)
        return ids

    for name, v in vulns.items():
        if v.get("severity") in FAILING:
            for aid in walk(name, set()):
                found.setdefault(aid, set()).add(name)
    return found


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else ".github/npm-audit-allow.json"
    today = dt.date.fromisoformat(os.environ.get("TODAY_OVERRIDE") or dt.date.today().isoformat())
    allowed, errors = load_allow(path, today)
    report = json.load(sys.stdin)
    blocking = 0
    for aid, pkgs in sorted(failing_advisories(report).items()):
        if aid in allowed:
            print(f"accepted {aid} until {allowed[aid]} ({len(pkgs)} packages): {', '.join(sorted(pkgs))}")
        else:
            blocking += 1
            print(f"FAIL {aid} ({len(pkgs)} packages): {', '.join(sorted(pkgs))}")
    for e in errors:
        print(f"FAIL {e}")
    if blocking or errors:
        print(f"npm audit gate: {blocking} advisory(ies) not accepted, {len(errors)} allowlist error(s)")
        return 1
    print("npm audit gate: no unaccepted high or critical advisory")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
