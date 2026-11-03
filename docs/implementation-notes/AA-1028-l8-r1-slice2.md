# AA-1028 R1 slice 2 (AA-Partner) — crm_entitlement_pull: the CRM projection of tenant.entitlements.v1

Lane l8-r1-slice2 of S266. Root ADR 0019 d1 / d2; design: root `docs/audits/2026-11-03-S265-ecosystem-restructure/
README.md` §3.5 (CRM consumer). Two PRs: the migration-only PR (crm-043, MIG-4) and the code PR (the pull job, the
feature gate, crm.ops.v1 1.11).

## Decisions

- **crm-043 is one contract file**: a new platform singleton `crm.entitlement_feed_state` (same shape as
  `crm.membership_feed_state` plus `last_reconcile_at`), a nullable `crm.tenant_entitlement.projected_at`, and
  `crm.sync_tenant_lock_source()` restated on top of the crm-013 text so the account feed's actor
  (`platform:feed-tenant-account`) may name `cis_tenant`; `cis_membership` stays the membership feed's only.
- **A marker column, not a source enum.** `projected_at` NULL means "never projected from CIS" (what the L1 console
  wrote, and may still write); crm.ops.v1 1.11 serves it as `managed_by` (cis / ops).
- **`reconcile_failures` (fix pass 2, re-review minor 0)**: the hourly reconcile's own consecutive failing passes on
  `crm.entitlement_feed_state` (0-1,000,000). The event pull's success resets `failures` every run, so a reconcile that
  failed on the same agency every slot never escalated, never logged the stuck alarm and re-ran the full pass every
  2 minutes. crm-043 is not applied anywhere yet, so the column is added in place (same file in both PRs).
- **`test_crm013_schema` re-applies crm-043 after re-running crm-013** (`LATER`): the re-run restates the old
  `sync_tenant_lock_source()`, which made the account feed's lock refused (CRM21) for every later test of the session.

## Should know

- Apply on Dev: the bot copy to AA-CIS-Infra `db/migrations/crm/`, then Infra Migration Apply (contract, every job table
  idle) or a DB Migrate apply of `crm/043_crm_entitlement_feed.sql`; then the code PR (it raises readiness to crm-043).
- DM-7.1 row for `crm.entitlement_feed_state`: root `docs/architecture/data-standards/02-data-management.md` (root PR of
  the same set, merged before the Infra copy).
