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
- **crm.ops.v1 1.11 is additive**: `Entitlements.managed_by` (cis / ops) is a new field. Fix pass 2 (re-review
  finding 2): `PUT /entitlements-allowed` keeps writing while the agency is `managed_by` ops (never projected: an agency
  with no CIS tenant, such as the CRM-seeded E2E agencies 'Lữ hành Sen Vàng' bab16039-a064-517f-a99f-172b729ad3c4,
  southern-cross and harbour-lights, or one the adoption guard holds) and answers 409 `entitlements_managed_by_cis`
  (audited `denied`, nothing written) only once the pull projected the row. The check and the write are one statement
  (`ON CONFLICT ... DO UPDATE ... WHERE projected_at IS NULL`; no row back = projected), so a pull that projects the row
  concurrently wins and the console answers 409. Without this the always-409 PUT left an agency without a CIS tenant
  with no way at all to change its features (and the new `whatsapp_sms` gate would have cut its SMS for good).
- **Reconcile goes past a failing agency (fix pass 2, re-review minor 0).** A `_FeedError` of one agency in the hourly
  pass is counted (`reconcile_failed_count`) and the pass goes on; the pass then records `reconcile_failures + 1` and
  `last_reconcile_at`, and the next pass is due after 5, 10, 20, 40 minutes (then hourly) instead of every slot; a
  clean pass resets the count; 5 failing passes in a row log `crm_entitlement_feed_stuck part=reconcile`. The event
  pull's own error handling (stop before the cursor moves) is unchanged.
- **`app.entitlements.FEATURES` is the one list** (the console imports it).

### Tradeoffs

- One snapshot GET per changed tenant per run (tens of tenants on Dev) against an incremental apply of event bodies: the
  snapshot is always the truth and needs no inbox table or erase registration.
- The two CIS feeds may both lock an agency (`cis_tenant`); both follow the same CIS status, so they converge.

### Should know

- Order: crm-043 applied on Dev (DB Migrate) -> **the CIS saga PR deployed** -> this PR (readiness requires
  crm-043). The CIS saga must be live first so that, once this pull projects an agency, the CIS path to change its
  features exists (nothing enforces the order across repos: the PR bodies state it).
- No revoke path in this slice (re-review minor 1): once CIS manages an agency, removing a CRM feature or cancelling
  the add-on needs a CIS path that does not exist yet (the saga only adds or changes add-ons; no admin endpoint writes
  `shared.tenant_entitlement_override`). Slice 3 adds it; until then a partial revoke is a saga attach of a crm plan
  without the feature, or a lock. An agency still `managed_by` ops is changed on the L1 console as before.
- CIS must serve `/tenant/v1` (AA-CIS-App producer PR + AA-CIS-Infra gateway route) for the pull to do anything;
  until then it records `cis_not_found` / `cis_forbidden` and backs off, harmlessly.
- The tenant.entitlements.v1 contract is not vendored here yet (the membership one is, with a hash pin); `parse_account`
  checks the fields this consumer reads and ignores unknown ones.
- Tests that need `ai_assist` write it through `entitlement_feed.apply` (the projection path), not the L1 console.

### Review fixes (S266 fix pass)

- Adoption guard and the two extra `whatsapp_sms` checks above; unit tests (`test_entitlement_feed.py`,
  `test_workflows_engine.py`, `test_messaging_sender.py`) and integration tests (`test_crm043_entitlement_feed.py`
  adoption case, `test_messaging_db.py` queued text after the feature was removed).
- Live projection check after deploy: use a CRM agency that IS a CIS tenant (Atlas & Hearth
  9149d3c6-fcaf-4400-8e7a-4ed5e557e146): attach crm.core with the CIS saga, then ai_assist is projected
  (`managed_by` cis, the L1 PUT answers 409) and `whatsapp_sms` answers 403 (crm.pro only). Not 'Lữ hành Sen Vàng':
  it has no CIS tenant (the saga answers tenant_not_found, the pull ignores its 404), so it stays `managed_by` ops and
  its features are set on the L1 console.
