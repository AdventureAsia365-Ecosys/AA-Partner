# AA-1176 — Dev-only E2E owner (root ADR 0006a A10h), AA-Partner part

## Decisions
- The E2E owner (`e2e-owner@e2e-mail.lumiguides.it.com`, groups exactly `cis-admin` + `booking-admin`) is a **plain
  `cis-admin` operations reader** in the CRM: no `e2e_read_only`, no `e2e_no_write` flag. CIS relays the staff member's
  own token to `crm.ops.v1` for the Compliance Refresh evidence (`crm_ops`), which is one of the live steps; it needs a
  plain reader. It holds no `crm-platform-admin`, so CRM writes refuse it with `missing_group` like any CIS admin.
- **Guard** (`route_guard.guard_for`, same place and detail as A10c / A10d): a WORKFORCE token whose `email` claim trims
  and lower-cases to the address is refused 403 `e2e_read_only` outside `DEV_ONLY_ENVIRONMENTS` (dev / test / local;
  the deployed values are dev / stg / prod, so only Dev passes), with any group set other than exactly
  `cis-admin` + `booking-admin`, or with a non-ASCII raw value (`e2e_staff.e2e_owner_allowed`).
- **MFA exemption**: it joins the `e2e=` flag of `admin_mfa.check` (its code is readable only by the MFA admin role).
- **Never a studio invitee**: `is_e2e_mailbox_address` excludes it with the other staff E2E addresses.

## Changed
- `api/app/auth/e2e_staff.py`, `api/app/auth/route_guard.py`, `api/tests/unit/test_aa1176_e2e_owner.py`, `CONTEXT.md`.
- No migration, no setting.

## Tradeoffs
- `DEV_ONLY_ENVIRONMENTS` includes `test` / `local` (the existing A10c / A10d rule); neither is a deployed environment,
  so the identity still exists only on Dev. CIS and Booking compare `ENVIRONMENT == "dev"` exactly.

## Should know
- Removal with the A10 family (AA-1054): the constants, `is_e2e_owner_like`, `e2e_owner_allowed`, the guard block, the
  MFA term and the invitee exclusion.
