# AA-979 — crm.ops.v1 studio-invitation write guard for the Dev-only E2E CIS admin (A10b)

Session S252, agent Claude Code (subagent). Branch `fix/crm-ops-studio-invite-write-guard`. Finding of the S252
security review of the A10b studio-invite change (pre-existing since AA-979 PR #74): `POST
/crm/ops/v1/tenants/{id}/studio-invitations` takes any OPS caller and knows nothing about synthetic tenants, so a
leaked A10b code could call the CRM directly (bypassing the CIS checks of AA-CIS-App PR #1036, root ADR 0006a A10b-1),
re-send a real tenant's invitation (an owner one included) to the E2E mailbox and claim it.

## Decisions

- **How the CRM recognises A10b.** Not by a group: the A10b identity has no group of its own. The WORKFORCE pre token
  trigger (AA-CIS-Infra `accounts/aa365/lambda_src/identity_triggers/workforce.py`, `E2E_DEV_ONLY_TEST_STAFF`) issues
  its tokens with exactly `cis-admin` (and refuses to issue any if the account holds another group), the same group
  as a real CIS admin. What tells them apart is the access token's `email` claim, which the same trigger sets on every
  WORKFORCE access token (`claimsToAddOrOverride: {"email": …}`, signed by the pool). The CRM guard compares it with
  the code constant `e2e-cis-admin@e2e-mail.lumiguides.it.com` (trimmed, lower-cased, like CIS
  `api/core/e2e_staff.is_e2e_cis_admin`) and puts a boolean `Caller.e2e_read_only` on the caller (WORKFORCE issuer
  only). No address is kept on the caller.
- **"Write-capable OPS caller" = any OPS caller except A10b.** Making the relay `crm-platform-admin`-only would break
  every real CIS admin (orchestrator decision Q4 lets `cis-admin` relay). So the relay stays open to every OPS caller
  and only the A10b caller gets the extra terms. Every other ops write already needs `crm-platform-admin`;
  `require_group` now also refuses A10b there with 403 `e2e_read_only` (a second lock: the trigger never issues it
  that group), and `/console` answers `can_write: false` for it.
- **How the CRM knows the terms (the option picked).** Considered: (a) a CIS-signed relay assertion — the only way
  for the CRM to see CIS's own `is_canary` check, but it needs a new key or M2M scope (Terraform), CIS code and a
  contract change; (b) refuse A10b outright in the CRM — simplest, but closes the phase 2 automation path root ADR
  0006a A10b-1 just opened (the CIS route would create the membership and then fail the relay); (c) checks on data
  the CRM can already reach. Picked (c), fail closed:
  1. environment in `dev` / `test` / `local` (the synthetic-only environments of `app.agencies`);
  2. address on the E2E mailbox domain (`e2e-mail.lumiguides.it.com`, exact, ASCII checked before lower-casing,
     the A10b address itself refused) — same rule as CIS;
  3. **synthetic = the CRM agency flag**: `crm.tenant.is_synthetic IS TRUE` for that id, not trashed
     (`tenancy.synthetic_agency`, one read-only transaction under the crm-002 RLS). No CRM agency = refused;
  4. **role and status from CIS**, not from the body (the body has no role): `tenant.membership.v1`
     getTenantMembers with the CRM's existing M2M client (scope `cis.tenant/read`, already granted for the
     membership feed; base URL + tokens of the activation config). The membership with that `invitation_id` must be
     in that tenant, `status = invited`, role `viewer` / `editor`. A CIS failure = 503 `cis_unavailable`.
  Every refusal is 403 `e2e_read_only` (the detail CIS uses, so the BFF and the phase 2 spec need no new string);
  the reason is in the log line `crm_studio_invitation_e2e_refused reason=environment|address|tenant_not_synthetic|
  invitation_not_found|invitation_status|role` (ids only, never the address).
- **Order**: cheap and local first (environment, address, synthetic read), CIS last, so a real tenant never causes a
  CIS call; all before `studio.issue` opens its transaction, so a refusal writes no row and sends no mail.

## Changed

- `api/app/auth/e2e_staff.py` (new): the address, domain, roles, `is_e2e_cis_admin`, `is_e2e_mailbox_address`.
- `api/app/auth/route_guard.py`: `Caller.e2e_read_only` (default False), set for WORKFORCE tokens; `require_group`
  refuses it with 403 `e2e_read_only`.
- `api/app/cis/invitation_lookup.py` (new): getTenantMembers lookup of one invitation (`found` / `missing` / `error`).
- `api/app/tenancy.py`: `synthetic_agency()` (+ `SYNTHETIC_AGENCY_SQL`); not a context factory, so the
  `test_code_rules` context ownership is unchanged.
- `api/app/members/studio.py`: `check_e2e_invitation()`.
- `api/app/routers/ops.py`: the relay runs the check for an A10b caller; `/console` `can_write` false for it;
  module docstring.
- `CONTEXT.md`: OPS route-class row and the "Content studio invitations" section.
- Tests: `api/tests/unit/test_ops_e2e_staff.py` (new, 29 cases), one integration case in
  `api/tests/integration/test_studio_invitations_db.py` (`synthetic_agency` as crm_app under RLS: synthetic True,
  real False, unknown id False).

## Tradeoffs

- **The CRM's synthetic flag is weaker than CIS's `is_canary`.** On Dev the CRM refuses `is_synthetic = false`
  (ADR 0010 decision 3), so on Dev check 3 means "an AA platform admin registered a CRM agency with this id". A leaked
  A10b token cannot create one (POST /tenants needs `crm-platform-admin`), and every creation is audited with a
  ticket; but if a CRM agency was ever created on Dev with the id of a real CIS tenant, A10b could target that tenant's
  viewer / editor invitations (never owner: check 4). Stronger follow-up, if wanted: `tenant.membership.v1` 1.2 adds
  `is_synthetic` (CIS `shared.tenants.is_canary IS TRUE`) to the `TenantMembers` snapshot, and check 3 reads it from
  the same CIS call (a small CIS change + contract bump; no new secret).
- One extra CIS call per A10b relay only (a few per phase 2 run); no caching, so a role change at CIS applies at once.
- Kept the relay route open to `cis-admin` (Q4) rather than moving it behind `crm-platform-admin`.

## Should know

- The phase 2 E2E target tenant (`E2EP2TENANT`, mode `a10b`) now also needs a **synthetic CRM agency with the same
  id** (L1 console "create agency", POST /crm/ops/v1/tenants, `is_synthetic: true`, or the Dev seeder); without it
  the CRM answers 403 `e2e_read_only` and the CIS invite returns a failed delivery.
- The A10b detection relies on the WORKFORCE pre token trigger putting `email` into access tokens (it does on
  AA-CIS-Infra main). A token without the claim would be treated as a normal `cis-admin`.
- Contract text changes (crm.ops.v1: the relay is no longer "any OPS caller" for A10b; new 403 / 503 codes):
  root worktree `root-ops-invite-guard` (branch `docs/crm-ops-studio-invite-a10b-guard`), a local commit, no version bump
  (no shape change, new error codes only); the root contracts checker passes.
- No migration, no Infra change, no new secret or scope.
