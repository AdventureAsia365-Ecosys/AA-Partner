# AA-1093 stage 2 (S281 lane l2-erase-crm) — the CRM step of the CIS tenant erasure saga

Plan: root `docs/audits/2026-11-13-S279-session/aa1093-plan.md` (Part 2, decisions D2, D7, D8). Contract:
`tenant.erasure.v1` 1.0 (byte copy in `docs/contracts/`, pinned by `tests/unit/test_aa1093_tenant_erase.py`). Stage 1
(CIS, Booking, TripPlanner) ran for real in S280 on a synthetic tenant; the request then waited for the CRM step, which
did not exist. Two PRs: crm-066 / crm-067 (migration-only, MIG-4), then this code.

## Decisions

- **One job kind, `crm_tenant_erase`**, a due-now hook every 2-minute slot (as `crm_entitlement_pull`): each run pulls
  the CIS feed (cursor in `crm.tenant_erasure_feed_state`) into `crm.tenant_erasure_saga` and then works every request
  whose crm step is active. No queued event rows per request: the stored request is the work list, and a run that
  stops half way finds the same state next slot.
- **The report is the record.** `crm.tenant_erasure_report` (one row per request, round, mode) is written in the
  transaction that did the work: an apply's report commits with the erase, so a retry never recomputes a plan over
  erased rows (that would read as `plan_changed`). It is posted after the commit and posted again with the same
  `report_id` and body until CIS answers; a 4xx with a problem code settles it (`refused:<code>`).
- **Plan hash in the contract form, digests null** (as CIS's own plan): the CRM erases every row of the agency, so
  there is no row selection to bind; the agency is out of service since T0; a digest over feed-written tables (the
  membership and Booking inboxes, the hourly entitlement projection's `updated_at`) would change between the DRY RUN
  and the apply with nothing new to erase. Counts, the tenant and the round bind the plan.
- **Plan entries**: every count of `crm.erase_subject('tenant')` in DRY RUN (`delete`; `traveler_referrer_unlinked`
  as `crm.traveler detach`; `document_object` left out: the bucket objects of the counted `crm.document` rows), the
  keys the crypto-shred destroys (`crm.tenant_key shred`), the agency row whose name is replaced
  (`crm.tenant anonymise`), the tenant's studio invitations (`crm.studio_invitation delete`).
- **The agency row keeps no name** (`Erased agency <first 8 of its id>`, as Booking's partner row, D3): the row stays
  (crm-010 keep list, status `offboarded`).
- **Studio invitations** (`crm.studio_invitation`, keyed by `cis_tenant_id`, out of `erase_subject`'s reach) hold the
  invitees' identity (`cognito_sub`) and sealed addresses: erased through a SECURITY DEFINER function
  (`crm.erase_studio_invitations`, crm-067, actor `job:crm_tenant_erase` only, EXECUTE `crm_eraser`), not a DELETE
  grant to crm_app: the crm-020 row policy also lets two other job actors through, and a grant would give them DELETE.
- **Guards** (each app enforces them itself, D8): the pilot (the request's copy or the agency's own marker) in every
  mode; outside prod only a synthetic tenant is applied (the request's copy, and the agency's own marker when the CRM
  holds one); the kill switch `TENANT_ERASE_APPLY_ENABLED`; an approved hash for this round before its expiry. A
  DRY RUN needs no switch.
- **The owner's own flow** (AA-942 / AA-967) stays: while the owner's erase runs (agency `offboarding`) the saga is
  `blocked` / `refused` with `owner_erasure_running`; the owner's open requests (requested, export ready, failed) are
  cancelled by the saga's apply. An agency the owner already erased (`offboarded`) is applied with the entries of its
  plan (review S281): the DRY RUN counts read again (all 0: `crm.erase_subject` runs only on an agency in
  `offboarding`, and the owner's erase left no row, CRM16; a count above 0 is `failed` `rows_left_after_offboarding`),
  a key still live destroyed, the name replaced (`crm.tenant anonymise 1`), the invitations deleted. The apply's
  tables equal the DRY RUN's.
- **One run per request at a time** (review S281): each work transaction starts with
  `pg_advisory_xact_lock(hashtextextended('crm_tenant_erase:' || request_id, 0))` and reads the round's report of the
  mode again under it (`saga_store.claim`). Two runs at once (a rolling deploy with WORKER_ENABLED on both tasks, a
  reaped lease, a run longer than its slot) never make a report twice: the second rolls back and posts nothing; the
  next run posts the stored report. An insert that meets the row also rolls back instead of posting an unrecorded body.
- **Bucket objects deleted last, inside the apply's transaction** (review S281, as `offboarding._erase`): after the
  report and audit rows, before the commit. A bucket error (`_ObjectsNotDeleted`, not a ServiceError) rolls back the
  rows, the key destroy and the report, and the worker retries the whole apply (a missing object counts as deleted).
  Before the fix the objects were deleted after the commit: a bucket error left the files with an `ok` report that no
  run would redo.

## Changed (vs the lane brief)

- "Kill switch / apply gate off by default like Booking's": an env flag set by Terraform (`TENANT_ERASE_APPLY_ENABLED`,
  default false), as Booking and TripPlanner. No CRM admin setting: S279 thresholds audit section 4 keeps destructive
  kill switches in Terraform; the second key is the tech lead's approval in CIS (and CIS's own
  `cis.erasure.saga_apply_enabled`). AA-CIS-Infra needs the variable on the CRM API task (follow-up PR, default off).
- `crm.studio_invitation` added to the scope (not in the plan's table list): it is CRM data of the CIS tenant.

## Tradeoffs

- One transaction for the whole apply (as the owner's offboarding): a large agency holds its locks for the run; the
  agency is out of service since T0, so no member waits on them.
- The census reads `pii:` columns from the catalog at run time (a new tagged column is covered without a code
  change); a table crm_app cannot read fails the verify (`census_incomplete`) instead of being skipped.
- No digest means a plan approved for a count can apply to different rows with the same count; acceptable for a
  whole-tenant erase, and the same rule CIS applies to itself.

## Should know

- MIG-4: crm-066 (expand) and crm-067 (contract) ship first in one migration-only PR (batch crm 066-067); this code
  raises readiness to crm-067 (`app/db.py`).
- Dev live order: deploy, Infra TENANT_ERASE_APPLY_ENABLED stays false for the DRY RUN; the real apply needs the
  Infra flag true for the CRM API, then back to false.
- Tests: `tests/unit/test_aa1093_tenant_erase.py` (rules, feed, posting, schedule, record SQL),
  `tests/integration/test_aa1093_tenant_erase_db.py` (full rounds on a real agency, plan changed, guards, a tenant
  without an agency, an agency the owner erased first, a bucket error that rolls the apply back, two runs at once on
  the work lock).
