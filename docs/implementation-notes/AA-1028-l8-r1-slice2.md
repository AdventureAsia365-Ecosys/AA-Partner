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

## Code PR: the pull, the gate, crm.ops.v1 1.11

### Decisions

- **Snapshot per named tenant, not event by event.** The account feed's change bodies are small and typed; the contract
  says a consumer that needs the full state re-reads the snapshot. The pull keeps no inbox: the projection is
  state-based and idempotent, so a replayed page writes nothing (unchanged projection = no write, no audit event).
- **A due-now hook every 2 minutes, not a Recurring kind.** A Recurring kind is always queued ahead and must be listed as
  idle-exempt in AA-CIS-Infra `owners.yaml` and `run_migration.py` (an Infra PR); a due-now row exists only while it runs
  (seconds), like the hourly catalog sync. Feature changes reach the CRM within about 2 minutes.
- **No snapshot call for a tenant the CRM does not hold** (`held()` first): most CIS tenants have no CRM agency.
- **Hourly reconcile of every agency** (`registry.agencies`): an agency created in the CRM after its tenant's last CIS
  event would otherwise wait for the next CIS event.
- **No CIS crm subscription = free, no feature, except before the first projection (review fix).** CIS is the only
  source (ADR 0019 d1), but CIS 355 backfilled no crm subscription, so a plain projection would wipe every existing Dev
  agency's `ai_assist` on the first run. Adoption guard: a row with `projected_at` NULL whose account has no crm
  subscription in any status is left as the L1 console wrote it (`awaiting_cis_count`, log
  `crm_entitlement_awaiting_cis`); the CIS lock still applies. Once CIS has a crm row (even canceled) the projection
  takes over; after that an account without one projects free. `crm.core` absent does not lock the agency (the CRM
  free plan stays usable; the nav by entitlement is R3).
- **`member_limit` kept when CIS gives no `crm.seats`** (a canceled add-on): never drops below what the agency had.
- **Status writes reuse the membership feed's rules**: lock only an active agency, lift only a `cis_tenant` lock.
- **`whatsapp_sms` gates connect, test and send**; reading the status, refresh and disconnect stay open so an agency
  that lost the feature can see and remove its connection. Review fix: the workflow `send_text` step
  (`skipped_feature_not_allowed`, `workflow_send` denied) and the dispatcher's `TextSender` (message `failed`
  `feature_not_allowed`, `message_send` denied, checked before the credential is read) check it too, so a connected
  Twilio account stops sending for automations and texts queued before the feature was removed. Web labels for both
  codes (EN / VI). `TEXT_FEATURE` lives in `app.messaging.state`. The other four codes have no feature to gate yet.
- **crm.ops.v1 1.11 is additive**: `PUT /entitlements-allowed` stays a route and answers 409
  `entitlements_managed_by_cis` (nothing read or written); `Entitlements.managed_by` (cis / ops) is a new field.
- **`app.entitlements.FEATURES` is the one list** (the console imports it).

### Tradeoffs

- One snapshot GET per changed tenant per run (tens of tenants on Dev) against an incremental apply of event bodies: the
  snapshot is always the truth and needs no inbox table or erase registration.
- The two CIS feeds may both lock an agency (`cis_tenant`); both follow the same CIS status, so they converge.

### Should know

- Order: crm-043 applied on Dev (DB Migrate) -> this PR (readiness requires crm-043).
- CIS must serve `/tenant/v1` (AA-CIS-App producer PR + AA-CIS-Infra gateway route) for the pull to do anything;
  until then it records `cis_not_found` / `cis_forbidden` and backs off, harmlessly.
- The tenant.entitlements.v1 contract is not vendored here yet (the membership one is, with a hash pin); `parse_account`
  checks the fields this consumer reads and ignores unknown ones.
- Tests that need `ai_assist` write it through `entitlement_feed.apply` (the projection path), not the L1 console.

### Review fixes (S266 fix pass)

- Adoption guard and the two extra `whatsapp_sms` checks above; unit tests (`test_entitlement_feed.py`,
  `test_workflows_engine.py`, `test_messaging_sender.py`) and integration tests (`test_crm043_entitlement_feed.py`
  adoption case, `test_messaging_db.py` queued text after the feature was removed).
- Deploy order still matters for the live 403 check: attach crm.core to the E2E agency with the CIS saga first (then
  ai_assist is projected), and show a 403 on `whatsapp_sms` (crm.pro only).
