# S291 lane f-crm-perf — CRM job worker database load (AA-838, C4.3 W48 findings 4 and 5)

Source: root `docs/audits/2026-11-24-S290-session/c43-w48/README.md` (findings 4 and 5) and its probe output.

## Decisions

- **The proposal lookup is the Booking sync's accepted-proposal step** (`app/booking/proposal_lines.py` `DUE_SQL`,
  called by `BookingSync.proposal_sets` for every agency on every minute run): about 60 agencies on Dev x 1 run a
  minute = the one call a second the probe saw. The same step also runs `CONFIRM_DUE_SQL` (1.68 M calls).
- **Cadence, not an index.** The tables behind `DUE_SQL` are tiny on Dev (`crm.proposal` ~110 rows, `crm.option`
  ~206, `crm.booking_line` ~61) and the existing partial unique index `proposal_one_accepted_uq (tenant_id, trip_id)
  WHERE status = 'accepted' AND deleted_at IS NULL` already matches the proposal predicate; a new index would not
  change the plan of a statement over a few dozen pages. The call count is the cost, so the step became event driven:
  a nudge run (accept, paid deposit, claim, re-hold all queue one, payload `tenant_id`) always checks; the minute run
  re-checks an agency only when its last check left work (a set due, a confirm queued or failed, an outage) or after
  `crm.jobs.booking_proposal_check_every_s` (default 300 s, the backstop for a nudge that never ran). No migration
  other than the settings seed.
- **Tenant status cache in job contexts only** (`system_tx` -> `_agency_tx` with `statuses=None`). The 6.4 reads a
  second are the Booking sync's ~6 `system_tx` per agency and minute run. Only `active` is cached, for
  `crm.jobs.tenant_status_cache_s` (default 10 s, 0 = off) from the read that saw it; any other status (and an unknown
  agency) is never cached and drops the entry. Bound: an agency locked / offboarded / trashed in any process is refused
  by every job context of this process at most that many seconds after the change commits (plus the length of a job
  transaction already open, as before). `tenant_tx()` (members), `platform_tx()` (AA staff), the feeds and the public
  contexts are unchanged (they read every time). No invalidation hooks were added to the lock / offboarding writers
  (the lane scope: no change to tenant / entitlement code beyond the cache); the TTL is the bound.
- **Prune cadence**: `Worker(prune_every_s=...)` (None = the ported template's "every pass"); the app passes
  `crm.jobs.history_prune_every_s` (default 60 s, minimum 60). The DELETE and each kind's `keep_days` are unchanged.
- **Finding 5 root cause = asyncpg savepoint names.** asyncpg names every nested `conn.transaction()` savepoint after
  a process-wide counter (`__asyncpg_savepoint_<hex>__`), and `pg_stat_statements` keys utility statements by text
  (`track_utility` on). Every `tenant_tx` / `system_tx` opens one savepoint, so each one added a new `SAVEPOINT` and
  `RELEASE SAVEPOINT` entry of one call: about 13 new entries a second at 6.4 contexts a second, which matches the
  ~18,600 `dealloc` runs (each evicts 5 % of `pg_stat_statements.max`) in 8 days. Fix: `app/pg_connection.py`, a
  `connection_class` for both CRM pools that names a savepoint by its depth (`crm_sp_1`, `crm_sp_2`, ...).
- **Depth names, not one fixed name**: `ROLLBACK TO` keeps the savepoint, so with one name an outer block failing after
  a caught inner rollback would roll back only to the inner savepoint. The open savepoint at depth d is always the
  newest one named `crm_sp_<d>`, so `RELEASE` / `ROLLBACK TO` by name reach exactly the block asyncpg means (unit test
  with a fake connection, integration test on PostgreSQL).
- **Restore drill**: one temp table name `rd_drill`, dropped after each table (`DROP TABLE pg_temp.rd_drill`); the
  `CREATE TEMP TABLE` text still varies with each table's column list (stable from run to run), the INSERT / count
  texts no longer vary with the manifest position.
- Other dynamic SQL texts checked: `SET LOCAL ROLE {reader}` (4 fixed names), `app/pipeline.py` UPDATE column lists
  and the portability builder (bounded sets, normalised), `app/audit_log.py` filters (bounded). None churns per call.
- `pg_stat_statements` settings untouched (Infra-owned).

## Changed

- `api/app/booking/sync.py`: `BookingSync.proposals_due`, `checked_proposal_sets`, counters `proposal_sets_due` and
  `proposal_check_skipped` in the job result; `run_agency` calls the checked step (`nudged = not feed`).
- `api/app/tenancy.py`: `_job_tenant_status`, `_agency_status`, `forget_job_status`; `_agency_tx` reads through them.
- `api/app/jobs/worker.py` + `api/app/main.py`: `prune_every_s`.
- `api/app/pg_connection.py` (new), `api/app/db.py` and the restore drill pool use it; `api/app/portability/restore_drill.py`
  fixed temp name.
- `api/migrations/088_crm_job_load_settings.sql` (contract, settings seed), compiled defaults, guides (EN + VI).
- Tests: `tests/unit/test_s291_crm_perf.py`, `tests/integration/test_s291_crm_perf_db.py`; the unit and integration
  conftests clear the status cache per test; the integration `app_pool` uses the new connection class (every
  integration test runs with depth-named savepoints); `test_slice1_routes.py` moves its clock past the cache window.
- `CONTEXT.md`: `system_tx` bullet, `crm_heartbeat` / `crm_booking_sync` rows, crm-088 row.

## Tradeoffs

- The proposal backstop means a nudge that is lost (its job row failed three times) waits up to 300 s instead of
  60 s; a Booking outage during a nudge run keeps the agency "not clean", so the next minute run retries as before.
- The status cache lets a job context run up to 10 s after a lock. Member and staff paths are unaffected.
- The connection class relies on two asyncpg internals (`Transaction.start` calling
  `Connection._get_unique_id("savepoint")` and `Transaction._state`); asyncpg is pinned (0.31.0) and the unit tests
  fail if a bump changes either.

## Measured locally (PostgreSQL 16 + pg_stat_statements, track_utility on)

Scratch script (not committed): 10 simulated minutes of the scheduled Booking sync over every agency plus a worker
maintenance pass every 6 s, `main` code vs this branch, counts from `pg_stat_statements` (role `crm_app`):

| | before (61 agencies) | after (81 agencies) | per agency-minute before -> after |
|---|---|---|---|
| `DUE_SQL` calls | 600 | 160 | 0.98 -> 0.20 |
| tenant status reads | 3,010 | 810 | 4.9 -> 1.0 |
| job prune DELETE calls (one process) | 300 | 30 | 30 -> 3 a minute (3 kinds, once a minute) |
| SAVEPOINT / RELEASE entries | 4,750 (capped by `pg_stat_statements.max` 5,000 locally) | 2 | |
| `crm_app` entries in total | 4,772 | 24 | |

A separate check: 200 nested transactions made 400 distinct entries with asyncpg's names and 2 with depth names
(PostgreSQL 16.15 does not normalise savepoint names either).

## Should know

- Live check after deploy: `.tmp-session/S291/aa838_s291_crm_load_counts.py` in the root workspace (DB Migrate
  `mode=run-script`, read-only, counts only) prints calls per minute of the three statements and the `crm_app`
  savepoint entry counts; the old `__asyncpg_savepoint_*` entries fade as they are evicted.
- The next C4.3 probe should see `crm_app` statement count fall from ~8,900 to a few hundred and `dealloc` stop growing.
- The same asyncpg savepoint naming exists in AA-Booking and AA-CIS-App (both asyncpg, nested transactions): a
  follow-up, not in this lane.
