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
- **G9 review fix: the former pilot stays real.** Reading the marker alone reopened the agency once CIS ends the pilot
  (the end-pilot route clears `is_pilot`; the agency still holds the real agency's data and `is_synthetic` true, as
  every Dev agency). `crm_entitlement_pull` now clears `crm.tenant.is_synthetic`, one way only, the first time it
  sees the marker (in the snapshot, or already stored for a pilot marked before this change), audit
  `tenant_real_marker` reason `cis_pilot`. That is the CIS view too: the pilot's `is_canary` is false and stays false
  after the pilot ends. Chosen over a new projected column or a sticky "was pilot" column: no migration (no MIG-4
  chain), the existing flag already means "real agency", and the console's test-agency delete (`not_synthetic`) is
  closed by the same write. Rejected alternative: make CIS refuse to end a pilot that is not offboarded; the CRM
  only locks an agency CIS offboards, and A10c's `ops_tx` opens locked agencies, so the order alone does not close it.

## Changed

- `api/app/seed/catalogue.py`: `REHEARSAL_AGENCY` and the agency.
- `api/app/tenancy.py`, `api/app/auth/e2e_staff.py` (docstring): the pilot exclusion.
- `api/app/cis/entitlement_feed.py`: `SET_REAL_SQL` and the real marker in `_apply_in` (review fix).
- Tests: `tests/unit/test_seed.py` (the id CIS holds, its members), `tests/unit/test_staff_e2e_crm.py` (SQL guard),
  `tests/unit/test_ops_e2e_staff.py` (fake SQL key), new `tests/integration/test_aa1086_e2e_pilot.py` (E2E staff
  refused on the pilot, lists skip it, the relay fact is false, normal staff unchanged, and still refused after the
  marker is cleared), `tests/unit/test_entitlement_feed.py` (the real marker: set with the pilot marker, set for a
  stale pilot when the pilot ends, never for an ordinary agency, never back to true).
- CONTEXT.md: route classes row, AA staff console, Dev seeder.

## Tradeoffs

- The rehearsal agency is created by every default `python -m app.seed --apply`. That is a few rows on Dev only
  (the seeder refuses other environments) and keeps one command for every E2E agency.
- No threshold, limit or cap added: nothing for the settings registry.

## Should know

- Live order (review fix): the Infra `crm_dev_identity.py --pool partner e2e-users --apply --write-secrets` (creates
  the two users, prints their subs), then `python -m app.seed --apply --agency pilot-rehearsal-01 --member
  pilot-rehearsal-01:owner:<sub> --member pilot-rehearsal-01:agent:<sub>` through ECS Exec on `aa-crm-dev-api`, then
  the CIS `rehearsal-tenant --apply`. The seed needs no CIS tenant; the other way round, `crm_entitlement_pull` skips
  the saga's events for an agency the CRM does not hold yet ("ignored") and projects it only at the hourly reconcile.
- After this deploys, the first pull that sees the Dev pilot (if one is marked) writes one `tenant_real_marker` audit
  event for it: expected, once.
- The CRM specs then run with `E2ECRMPARTNER` = the agency id and `E2ECRMEMAIL` / `E2ECRMPASSWORD` / `E2ECRMTOTP` from
  `crm/dev/e2e-test-crm/pilot-rehearsal-01-owner`.
