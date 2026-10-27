# AA-979 P2b §4.4 — remove the Dev EXTERNAL `cis-portal` bridge on `/crm/v1`

Runbook: root `docs/audits/2026-10-26-S252-aa979-p2b/RUNBOOK.md` §4.4 (design §3 step 11). Session S254, agent Claude
Code. Branch `feat/aa-979-remove-cis-portal-bridge`. Infra half: AA-CIS-Infra `feat/aa-979-remove-cis-portal-bridge-env`
(drops `COGNITO_EXTERNAL_ISSUER` / `COGNITO_CIS_PORTAL_CLIENT_ID` from `crm_app.tf`). Order: this code first, then the
env.

## Decisions

- **Caller check before removal** (S254, every repo on its latest `origin/main`): the only `/crm/v1` client is the
  AA-Partner web BFF (`web/lib/bff.ts`, `web/lib/crmApi.ts`), which sends its own session token; that session is a
  partner-pool `partner-web` token since AA-CIS-Infra `crm_partner_signin_enabled = true` (AA-939). The Content studio
  (`portal/`) calls CIS, never `/crm/v1`. AA-CIS-App calls only `/crm/ops/v1` (`cis-admin`); AA-TripPlanner-Web and
  AA-Booking have no `/crm/` call; the E2E suites sign in through the web (partner pool + TOTP since S253). No caller
  left, so the bridge goes.
- **Removed, not disabled**: the `(EXTERNAL, cis-portal)` pair leaves `AGENCY.accepts` and `identity_from_settings`; the
  `EXTERNAL` / `CIS_PORTAL` constants and the two settings fields are gone. Nothing configures the EXTERNAL pool any
  more, so an EXTERNAL token is unknown to the verifier: **401** `invalid_token` on every class (not 403: the API cannot
  tell it from any foreign issuer, and does not need to).
- **The env names are ignored, not refused.** The running task still has `COGNITO_EXTERNAL_ISSUER` /
  `COGNITO_CIS_PORTAL_CLIENT_ID` until the Infra PR is applied; refusing them would stop the deploy. `load_settings` no
  longer reads them. The old "Dev-only bridge" refusal on stg / prod went with them; `PORTAL_OTP_DEV_ECHO` keeps its
  Dev-only check (`DEV_ONLY_ENVIRONMENTS`, renamed from `PORTAL_BRIDGE_ENVIRONMENTS`).
- **Seeder**: the member issuer is the partner pool or `--issuer`; no EXTERNAL fallback.

## Changed

- `api/app/auth/identity_config.py`, `api/app/settings.py`, `api/app/seed/__init__.py`, `api/app/routers/agency.py`.
- Tests: `tests/tokens.py` keeps an EXTERNAL pool stand-in only to mint refused tokens; `test_route_guard.py` (new: a
  cis-portal token is 401 on every served path of every class; the retired env is ignored and partner-web still 200);
  `test_settings.py`, `test_seed.py`, `test_tenancy.py`, `test_agencies.py`, integration `test_crm004_directory.py`,
  `test_crm005_intake.py` move their callers / issuers to the partner pool.
- `CONTEXT.md` (route classes, verifier rules, seeder, env list), `README.md`.

## Tradeoffs

- 401 rather than a specific 403 `retired_client`: keeping the EXTERNAL issuer configured just to name its refusal
  would keep the EXTERNAL pool's JWKS in the API for no caller.

## Should know

- Live check after deploy (Dev): a `cis-portal` token on `/crm/v1/memberships` -> 401; a partner-pool session (the web)
  still works (Deploy Dev post-deploy verify, Fargate UI smoke).
- `crm.member` rows whose `cognito_iss` is the EXTERNAL issuer (pre-AA-939 synthetic members) can no longer match any
  token; they were already unreachable since the web moved to the partner pool.
- AA-CIS-Infra `tools/identity/crm_dev_identity.py` still defaults to `--pool external` (e2e users, web secret); that
  is tooling for the retired path, worth flipping to `partner` in a later Infra change.
