# AA-1032 part 3 — faster CI (AA-Partner)

Lane `ship-ci-speed` (S263). The same shard script and the CIS side are in AA-CIS-App
(`docs/implementation-notes/AA-1032-ci-speed.md`).

## Measured before (11 green `main` runs of CI, 01/11/2026)

| Job | Job time | Test step |
|---|---|---|
| Integration Tests | 6.0-9.3 min | 326-523 s (801 tests in one process) |
| Unit Tests | 2.9-4.6 min | 157-257 s (one process) |

Per-file timing from the log of main run 41300896544: `test_trip_cancel_api.py` alone took ~296 s of the 453 s, every
one of its 21 tests 17-33 s. Run alone it takes 12 s.

## Decisions

- **Root cause first: `test_trip_cancel_api.py` swept every agency.** Its `_sync` (imported from
  `test_aa_lines_api`) ran the whole `BookingSync` job with an empty payload, which walks every agency the session has
  created so far (hundreds by the time this file runs, `registry.agencies`) and sends their leftover requests to this
  test's fake. cProfile of a full local run: 1.87M `run_agency` resumptions, nearly all from this file. The file now
  has its own `_sync` that runs the job's per-agency step (`BookingSync.run_agency(..., feed=True)`) for the test's
  agency only. No assertion changed; an error of that agency's run now raises instead of being counted as
  `agency_failed` (stricter). Other files keep the shared helper (they run early, the sweep is cheap there).
  Local full suite: 310 s -> 148 s, 810 passed.
- **Integration: 3 matrix shards of consecutive ranges** (`.github/scripts/ci_shards.py files --contiguous`, a byte
  copy of the CIS script), each with its own Postgres service; each shard bootstraps the roles and its session fixture
  applies every migration, as the single job did. Consecutive ranges because CIS showed integration modules may lean on
  their predecessors; here a file keeps its predecessors except at a shard start. Local check on three fresh clusters:
  314 + 383 + 113 = 810 passed, 0 skipped (same as the single run). The no-skip guard runs per shard.
- **Required check name unchanged.** `Integration Tests` is now an aggregate job: `needs:` the matrix,
  `if: ${{ !cancelled() }}` (a failed shard must turn it red, not "skipped", which counts as passed), first step fails
  unless the matrix result is `success`. It also prints each shard's seconds and uploads `test-durations-integration`.
- **Unit Tests on 4 pytest-xdist workers** (`-n 4 --dist loadfile`, pin `pytest-xdist==3.8.0` in requirements-dev.txt,
  pip-audit clean). Local: 3133 tests, the same per-file coverage table as the single process (diffed), TOTAL 91.69%,
  180 s -> 58 s. Not sharded across jobs: one job is enough at that size and keeps the 90% gate in pytest-cov.

## Changed

- `.github/workflows/ci.yml`: `test-unit` adds `-n 4 --dist loadfile`; `test-integration` -> `test-integration-shard`
  (matrix 1..3) + `test-integration` (aggregate).
- New `.github/scripts/ci_shards.py`, `.github/test-durations/integration.json`, `api/tests/unit/test_aa1032_ci_shards.py`.
- `api/tests/integration/test_trip_cancel_api.py`: local per-agency `_sync`.
- `api/requirements-dev.txt`: `pytest-xdist==3.8.0`.

## Tradeoffs

- The durations file came from a run where `test_trip_cancel_api.py` still took ~296 s; its entry is set to 10 s (the
  measured time after the fix). Refresh the whole file from the `test-durations-integration` artifact of a green main
  run once this lands.
- 3 shards each pay ~15 s of Postgres start and ~5 s of migrations again; still far below the time saved.

## Should know

- A shard red at its first files while the single order passed: the module leans on an earlier module's leftovers;
  fix its setup rather than moving files.
- Shard count changes: the matrix list, `INTEGRATION_SHARDS` and the `/N` in the job name together
  (`test_aa1032_ci_shards.py` checks them).
