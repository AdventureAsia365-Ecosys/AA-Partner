# AA-1086 lane l16: pilot rehearsal prerequisites G1 (CRM part) and G9 (S279)

The pilot rehearsal plan (root `docs/ops/runbooks/pilot-rehearsal.md`) needs a synthetic agency with its own E2E owner
and agent (G1), and the onboarding runbook needs the Dev-only E2E staff check to leave the pilot agency out before the
pilot holds real data (G9). The CIS side (the `rehearsal-tenant` fixture, the end-pilot route) is in AA-CIS-App, the
two E2E users in AA-CIS-Infra `tools/identity/crm_e2e_users.json`.

## Decisions

- **G1: one more catalogue agency, not a new seeder mode.** `pilot-rehearsal-01` ("E2E Pilot Rehearsal Travel",
  en-AU, 6 travellers, roles owner + agent) joins `AGENCIES`. Its id is the same UUIDv5 the other agencies use, and
  AA-CIS-App provisions the CIS tenant under that id, so no id is copied between repos by hand. It sits in the default
  set because the Infra `e2e-users` tool prints one seeder command with every agency's `--member`; a separate list
  would make that command refuse ("not selected").
- **G9: the existing marker, no migration.** `crm.tenant.is_pilot` (crm-044) already follows CIS through
  `crm_entitlement_pull`, so the CRM knows the pilot without a new projection. `E2E_SYNTHETIC_SQL` (A10c staff
  contexts) and `SYNTHETIC_AGENCY_SQL` (the A10b-1 studio invitation relay) both read
  `is_synthetic IS TRUE AND is_pilot IS NOT TRUE`, the same rule as CIS (`is_canary` and not the pilot).

## Changed

- `api/app/seed/catalogue.py`: `REHEARSAL_AGENCY` and the agency.
- `api/app/tenancy.py`, `api/app/auth/e2e_staff.py` (docstring): the pilot exclusion.
- Tests: `tests/unit/test_seed.py` (the id CIS holds, its members), `tests/unit/test_staff_e2e_crm.py` (SQL guard),
  `tests/unit/test_ops_e2e_staff.py` (fake SQL key), new `tests/integration/test_aa1086_e2e_pilot.py` (E2E staff
  refused on the pilot, lists skip it, the relay fact is false, normal staff unchanged, an ordinary synthetic agency
  again once the marker is cleared).
- CONTEXT.md: route classes row, AA staff console, Dev seeder.

## Tradeoffs

- The rehearsal agency is created by every default `python -m app.seed --apply`. That is a few rows on Dev only
  (the seeder refuses other environments) and keeps one command for every E2E agency.
- No threshold, limit or cap added: nothing for the settings registry.

## Should know

- Live order: the CIS `rehearsal-tenant --apply` (orchestrator, at the start of the rehearsal), the Infra
  `crm_dev_identity.py --pool partner e2e-users --apply --write-secrets` (creates the two users, prints their subs),
  then `python -m app.seed --apply --agency pilot-rehearsal-01 --member pilot-rehearsal-01:owner:<sub>
  --member pilot-rehearsal-01:agent:<sub>` through ECS Exec on `aa-crm-dev-api`.
- The CRM specs then run with `E2ECRMPARTNER` = the agency id and `E2ECRMEMAIL` / `E2ECRMPASSWORD` / `E2ECRMTOTP` from
  `crm/dev/e2e-test-crm/pilot-rehearsal-01-owner`.
