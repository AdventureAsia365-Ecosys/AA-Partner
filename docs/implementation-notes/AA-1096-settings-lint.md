# AA-1096 — settings-lint over the whole CRM api, as its own "Settings Lint" check

S279 thresholds audit (root `docs/audits/2026-11-13-S279-session/thresholds-audit/`, section 5.4): each app repo has a
`settings-lint` guard so a new hard-coded threshold, limit or cap fails CI, and it is a required check. Lane L4 added
`api/tests/unit/test_aa1096_settings_lint.py` over a list of governed modules, run inside "Unit Tests". Done-when of
the issue: "`settings-lint` is a required check in each app repo".

## Decisions

- **The whole `app/` package is governed now**, not only the L4 list (payments, intake, lifecycle, rate limits, the AI
  gateway, proposals pricing, retention jobs, partner-account clean-up). A new policy-looking constant in any module
  (`MAX_*`, `*_DAYS`, `*_LIMIT`, `*_USD`, `*_TTL`, ...) now fails, unless marked `# technical-constant: <reason>` or
  made a setting. Same rule, same marker, same baseline file.
- **Vendored CIS byte copies are excluded** (`app/aa_llm/`, `app/auth/identity_verifier.py`,
  `app/platform_settings/reader.py`): AA-CIS-App lints them, and a marker added here would break the byte comparison.
- **Ratchet:** the 14/11/2026 findings of the newly governed modules joined the baseline (61 -> 360 entries, the 61
  L4 entries unchanged). From here the list only shrinks.
- **Own check name "Settings Lint"** (job `settings-lint` in `.github/workflows/ci.yml`), the same name in AA-Booking
  and AA-TripPlanner-Web, so branch protection can require it. Gated like Unit Tests (`needs: lint`, skipped on a
  docs-only PR, which counts as passed for a required check).

## Changed

- `api/tests/unit/test_aa1096_settings_lint.py`: `GOVERNED = ("app",)`, `EXCLUDED`, docstring.
- `api/tests/unit/snapshots/aa1096_settings_lint_baseline.json`: regenerated (only additions).
- `.github/workflows/ci.yml`: job "Settings Lint"; header lists it as non-required until the ruleset adds it.
- `api/tests/unit/test_s271_ci_scope.py`: the job list, and a test that the job keeps its name and Unit Tests' gate.

## Tradeoffs

- **Duplicate run.** The lint file also runs inside "Unit Tests" (about 2 s). A job of its own costs about a minute of
  runner time per PR (dependency install from the pip cache) and buys a check name that says what failed and that can
  be required independently.
- **Baseline, not classification.** The 299 new entries are not classified one by one here: many are page sizes,
  input bounds or job mechanics that deserve the marker; some are real policy still in code. A PR that touches one
  should mark it or move it to the registry, and the shrink check enforces that the entry then leaves the file.

## Should know

- Making "Settings Lint" a required check is a ruleset / branch protection change on `main` (orchestrator step).
- Regenerate the baseline only to remove entries: `python -m tests.unit.test_aa1096_settings_lint` (from `api/`).
