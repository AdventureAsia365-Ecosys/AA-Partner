# AA-1096 lane l5-settings-env (S281) — retire the one-release env overrides (CRM)

Last Done-when of AA-1096: "No env var remains for a migrated setting after one release". The release has passed (S279
settings shipped, S280 deployed again) and the CRM task definition sets none of the seven env vars (AA-CIS-Infra lane
L6). The AA-CIS-App lane does the same for CIS 428.

## Decisions

- **One PR, migration first in it (crm-068, contract).** The reader serves `app/platform_settings/compiled_defaults.json`,
  generated from the migration blocks (seed-parity test), never the table's `env_override` column. So the code change
  rides on the regenerated file, and the file cannot change without the migration. A code PR branched apart from the
  migration would be red until the migration merged, and nothing in it is independent of the migration file. No
  readiness raise (nothing reads the column), so the Migration preflight accepts it; a deploy before the Migration
  Apply changes nothing.
- **`settings-edit` block** (`api/scripts/gen_settings_defaults.py`): fields of existing keys, applied after the seed
  rows of the same file; unknown fields, `key` / `app` or an unknown key fail. crm-068 sets `env_override` null on the
  seven keys and drops the env var name from two descriptions; its post-check raises if any definition still names an
  env var.
- **The `Settings` fields go.** `ai_budget_max_usd`, `crm_llm_max_tokens`, `rate_limit_public_per_minute`,
  `rate_limit_intake_per_minute`, `rate_limit_intake_form_per_hour`, `rate_limit_member_per_minute`,
  `rate_limit_ai_per_minute`, their env parsing and bounds checks, `SETTINGS_ENV_OVERRIDES`, `settings_env_set` and
  `settings_env()` are removed; `app.main` no longer hands them to the reader. Nothing else read those fields: every
  limiter and the AI gateway already read the settings. The bounds now live only on the definitions (crm-064), where
  the API and the DB trigger enforce them.
- **The mechanism stays.** The reader's env step, `configure_env` (now also taking None = the process env), the 409
  `env_override_active` refusal and the "overridden by env" source are kept for a future one-release override.
- **Settings Lint rule 4** (`tests/unit/test_aa1096_settings_lint.py`, the required "Settings Lint" job): a retired
  key (`tests/unit/snapshots/aa1096_retired_env_overrides.json`) never gets an override back and no definition names a
  retired env var; no string literal in `app/` or `scripts/` (docstrings aside) is a retired env name; any future
  override needs a window in `tests/unit/snapshots/aa1096_env_override_window.json` (env, added, remove_by, issue; at
  most 14 days) and fails once `remove_by` has passed.

## Changed

- `api/migrations/068_crm_retire_settings_env_overrides.sql` (new), the generator, the compiled defaults.
- `app/settings.py`, `app/main.py`, `app/platform_settings/__init__.py` (docs, `configure_env(None)`), docstrings that
  named the env vars (`app/routers/public.py`, `client_pages.py`, `app/intake/public.py`, `app/auth/route_guard.py`,
  `app/llm/gateway.py`, `agency_ai.py`, `budget_limits.py`, `app/platform_settings/service.py`), `CONTEXT.md`.
- Tests: `tests/settings_serve.py` (new) serves setting values; `LocalPools.settings(**overrides)` and the health
  test client still accept the old field names and serve them as settings (`take_fields`), so ~15 test files keep their
  shape; `tests/unit/conftest.py` resets the served values per file. The mechanism tests lay the retired names back on
  their definitions for one test (`lay_retired_overrides`); a new test proves the old env names change nothing.
  Removed: the bounds tests of the removed env vars. New: `tests/integration/test_crm068_schema.py`. Lint baseline: two
  entries gone (`DEFAULT_LLM_MAX_TOKENS`, `DEFAULT_RATE_LIMIT_PER_MINUTE`).

## Tradeoffs

- Keeping the old keyword names in `LocalPools.settings` is a test-only convenience (a smaller diff); they no longer
  exist on `Settings`, and a test that builds `Settings(...)` directly with one gets a TypeError.
- The window rule can turn CI red on a date (by design: the override must go after one release). The window file is
  empty today.

## Should know

- **MIG-4 deviation, recorded (review S281).** The lane rule and root ADR 0017 MIG-4 ship a contract file in a migration-only PR, then the code PR. This branch ships the migration and the code in one PR on purpose: the seed-parity test (JSON = `api/scripts/gen_settings_defaults.py` over the migrations) and the replay test `api/tests/integration/test_crm064_schema.py` (DB `env_override` = compiled defaults) tie crm-068, the generator and `compiled_defaults.json` together, so a migration-only PR is red unless it also carries the regen, and the regen alone stops the reader honouring the env names, which turns the env-driven unit tests red unless they move too; a code PR branched from `origin/main` would then touch the same test lines and go DIRTY after the squash. The order MIG-4 protects still holds at runtime: no readiness raise (the Migration preflight accepts it), no code reads the `env_override` column, so a deploy before Infra Migration Apply changes nothing. The migration header says the same; the PR body names the chain as `applies:` on this one PR.

- Number crm-068: crm-066..069 belong to other lanes of this wave. If main max + 1 differs at merge, renumber (`git grep
  '068_'` and `'crm-068'`: the migration, its integration test, this file, the lint docstring, `CONTEXT.md`).
- AA-CIS-Infra comments in `modules/environment/crm_app.tf` / `crm_llm.tf` still describe the env vars as overrides;
  the Infra lane of this wave updates them.
