# AA-939 follow-up (S263): partner-account clean-up

Tech lead, S263 (01/11/2026): "clean up the e-mail accounts too". Deleting the synthetic test agency "test nghiep 2"
left the invited address's partner-pool account behind (never signed in, `FORCE_CHANGE_PASSWORD`).

## Decisions

- **Candidates come only from CRM invitation claims** (`crm.member_invitation.cognito_sub`). The clean-up never lists
  the pool, so an account nothing in the CRM created (e2e users, accounts made by hand) is never a candidate. Accounts
  bound only by a Content studio invitation are not candidates either (no agency audit to record them in; follow-up).
- **Keep rules** (any one keeps the account): a CRM member row `active` / `invited`, not trashed, in any agency (the
  task said active or invited; a `disabled` member signed in once, so Cognito says `CONFIRMED` anyway); a CRM
  invitation `pending` / `claimed` and not expired, or `accepted`; a studio invitation `claimed` and not expired,
  `accepted` or `activated`; any invitation (CRM or studio) that ended less than 7 days ago, except a CRM invitation of
  a trashed agency (a deleted test agency: ended for good). "Ended" = `updated_at` for `revoked` / `expired`,
  `deleted_at` for a trashed invitation row, `expires_at` for a `pending` / `claimed` row past its expiry that nobody
  marked expired yet.
- **Cognito is the last word**, inside the directory (`delete_never_signed_in`): the account is read again (ListUsers
  by `sub`) right before `AdminDeleteUser`; anything but `FORCE_CHANGE_PASSWORD` is `signed_in` (kept), and an
  account whose `UserLastModifiedDate` is after the last invitation ended is `changed_since` (kept): a claim racing the
  clean-up resends the temporary password, which changes the account. A `CONFIRMED` account can never be deleted by
  this code, whatever a caller decides.
- **Cross-agency reads** go agency by agency through a new context, `partner_account_tx()` (`app.tenant_id` +
  `tenant_isolation`, actor `job:crm_partner_accounts`), over every agency of the audit registry
  (`crm.audit_export_agencies()`, which keeps trashed agencies). It accepts trashed agencies (like `audit_export_tx`)
  and writes queued audit events at the end (like `system_tx`). No RLS bypass, no new definer function.
- **Studio rows**: `crm.studio_invitation` is a platform table whose policy lets only `platform:*` and the activation
  job read. Migration **crm-039** re-creates `studio_invitation_platform` with `job:crm_partner_accounts` in its USING
  clause only (read; its WITH CHECK and guard CRM37 refuse writes by it). Same policy name, so crm-020's "exactly three
  policies" guard holds on replay (lesson S243). `studio.account_links()` refuses to run (503 `cleanup_not_ready`)
  while crm-039 is not registered: without it the job would see no studio row and could delete an account a studio
  claim is about to use. crm-039 is also in `REQUIRED_SCHEMA_VERSIONS` (code PR).
- **When**: (a) `delete_test_agency` queues a `crm_partner_accounts` job (`{purpose: agency_delete, tenant_id}`) in
  its own transaction (runs only if the delete commits; retried by the worker if Cognito fails) instead of calling
  Cognito inside the request; (b) a daily due-now hook (`DailyPartnerAccountSchedule`, from `JOB_RETENTION_UTC_HOUR`),
  `{purpose: daily_schedule}`. Both read every agency; the agency run only takes identities with an invitation there.
- **Dry run**: `PARTNER_ACCOUNT_CLEANUP_DRY_RUN` (default true = counts only, `account_state` read, no delete). Infra
  sets it false on Dev (the tech lead's request). The CLI (`python -m app.members.accounts`) is a dry run unless
  `--apply`. Hard cap 50 deletes per run (`MAX_DELETES_PER_RUN`; `--limit` above it is cut); the rest is `capped`.
- **Audit**: one `partner_account_delete` event per agency whose invitation the account was (object
  `member_invitation`, object id = the first invitation, `object_ids` = all of them there, reason `never_signed_in`,
  actor `job:crm_partner_accounts` = kind `system`). The first agency's event is queued in the same context as the
  Cognito call and written only if it succeeded. Logs: ids and counts only, never the `sub` or the address.

## Changed (vs. the lane brief)

- The agency-delete clean-up runs as a job queued in the delete's transaction, not inline after it: the request stays
  fast, the job retries on a Cognito error, and nothing runs if the delete rolls back.
- No new ops route (`crm.ops.v1` unchanged): the live dry run / real run is the CLI in the container (the
  `app.audit_export.verify` pattern) or the daily job.
- Retention row for the partner pool users in root data standards Part B §7 (no new table).

## Tradeoffs

- Reading every agency every day is O(agencies) small transactions; fine at pilot scale. If it grows, an identity
  index policy (like `member_self_lookup`) on `crm.member_invitation` would let the job read only candidates.
- Accounts bound only through studio invitations stay (no agency to audit in). Follow-up if they pile up.
- `UserLastModifiedDate` as the race guard: Cognito sets it on RESEND; if it ever did not, the DB re-check is the
  window of one run (seconds) and the next claim recreates the account (a person sees "invalid password" once).

## Should know

- Order: crm-039 migration-only PR → Infra copy + apply (contract, ADR 0017a auto-apply when idle) → Infra IAM PR
  (AdminDeleteUser + alert + env) applied → this code PR. Before the IAM apply, a delete answers AccessDenied: the run
  counts it `failed`, goes on with the others, then fails the job so the worker retries it (3 attempts); the next daily
  run tries again; nothing else breaks.
- Live: the test address left by "test nghiep 2" is deleted at once if its claimed invitation belongs to that trashed
  agency. If its account was bound by a claim in another, live agency whose invitation ended in the last 7 days, the
  dry run says `kept_grace`; the operator may then run the CLI with `--grace-days 0` (every other rule still holds,
  `CONFIRMED` is still refused). The CLI flag is the only way to lower the grace; the jobs always use 7 days.
- Tests: `tests/unit/test_partner_accounts.py` (fake pool + MemoryDirectory + fake boto client),
  `tests/integration/test_partner_accounts_db.py` (real contexts, trashed agency, cross-agency member, live
  invitation, studio claim through crm-039), `tests/integration/test_crm039_schema.py`.
