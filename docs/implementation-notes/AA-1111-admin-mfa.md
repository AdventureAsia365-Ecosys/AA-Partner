# AA-1111 slice S2 — operations staff MFA check (lane l17, S285)

Tech lead decisions: admin MFA = TOTP (S283); drop the WAF geo rule of the staff hosts once admin MFA is enforced
(S285). CIS and Booking ship the same check behind their own switch.

## Decisions
- **Who is checked**: WORKFORCE sessions holding `crm-platform-admin`, `crm-ops` (the operations desk, break-glass group
  of support access) or `cis-admin` (reads every agency on `/crm/ops/v1`). Not checked: `crm-support` and the CIS desk
  groups (scoped, read-only), agency members (PARTNER pool already requires MFA), the Dev-only E2E identities.
- **Claim**: `amr` holds `otp` (TOTP). Read from the verified token string with PyJWT (no second signature check):
  adding `amr` to `Principal` would change the byte-copied verifier and chain three repos.
- **Switch**: `crm.platform.admin_mfa_required` (crm-082), bool, default off, maker-checker, impact high, no env
  override. Off = log mode (`staff_admin_mfa_check outcome=would_refuse`), so the claim can be proved on a real TOTP
  sign-in before it goes on.
- **One PR with the migration**, like crm-075 (#331): the reader serves the compiled default (off) until Migration Apply
  registers crm-082, there is no readiness raise, and the settings lint requires every compiled key to have a reader and
  every constant a compiled key, which a split code PR from `origin/main` could not satisfy. A migration-only PR
  would fail the other way: its compiled default (seed parity) would have no reader (dead-setting lint).
- **crm-082, not crm-081** (review fix): AA-1144 holds crm-081 (`api/migrations/081_crm_option_display_note.sql`,
  PR #360, the quote item display note), already on `main`. Guard 0 here requires crm-081 registered, the same way
  crm-081 requires crm-080. No readiness entry: like crm-075 and crm-077 (settings rows), the reader serves the compiled
  default until Migration Apply registers crm-082, so `app/db.py` REQUIRED_SCHEMA_VERSIONS stays unchanged.

## Changed
- `app/auth/route_guard.py` calls `admin_mfa.check` before building the caller; nothing is refused while the switch
  is off.

## Tradeoffs
- The log line is throttled (10 minutes per identity and outcome, a technical constant) instead of one per request.

## Should know
- A token with no `amr` (classic hosted UI, or the Cognito feature not on the pool yet) counts as "no MFA"; the Infra
  runbook's `staff-totp-check` proves the claim before the switch goes on.
- The CIS admin BFF relays `/crm/ops/v1` calls with the staff member's own token, so a refused session surfaces there as
  a 403 `mfa_required` from the relay.
