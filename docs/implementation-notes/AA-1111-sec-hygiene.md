# AA-1111 — security hygiene follow-ups (S294, lane l6-sec-hygiene)

Two small leftovers of the S284 headers slice (`AA-1111-headers.md`) and the S284 npm audit gate.

## Decisions

- **The picomatch acceptance is removed.** `npm audit --json` on the committed `web/package-lock.json` (25/11/2026)
  reports 0 vulnerabilities of any severity (`metadata.vulnerabilities.total == 0`; picomatch is still 4.0.7, the
  advisory no longer matches it). The gate (`.github/scripts/npm_audit_gate.py`) passes with an empty allow list, so
  `.github/npm-audit-allow.json` keeps its shape with `"allow": []`: the file stays the one place a future accepted
  advisory goes, and Workflow Lint keeps watching its path.
- **The CSP smoke in the standalone build job no longer pipes curl into `grep -q`.** Under `set -euo pipefail`,
  `grep -q` exits at the first match and closes the pipe; when curl has not finished writing it dies of SIGPIPE
  (exit 23) and pipefail fails the step although the header was there. The headers now go to
  `$RUNNER_TEMP/signed-out.headers` and grep reads the file. Same check, same header text.

## Changed

- `.github/npm-audit-allow.json`: entry for GHSA-fc5q-39xc-vjpf removed (`allow` is empty).
- `.github/workflows/ci.yml`: standalone smoke, `/signed-out` CSP header check.

## Tradeoffs

- Deleting the allow-list file was possible (the gate treats a missing file as empty), but an empty list keeps the
  documented place and the Workflow Lint path trigger.

## Should know

- The root `tools/github/ci_hygiene.py` gains WL9 in the same wave (root repo): it flags `curl ... | grep -q` in every
  repo's workflows and composite actions; the daily CI hygiene run reports it. This repo's line was the only one.
- If picomatch is flagged again, the gate fails closed; re-add an entry with a fresh check, reason and expiry.
