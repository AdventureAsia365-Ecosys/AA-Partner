# AA-969 follow-up (lane l10-export-purpose, S269): the export opens every sealed column under its writer's DEK

Found live in S269: the pilot agency's nightly backup (`crm_pilot_backup` -> `crm_data_export`) failed for good with
`decrypt_denied` once the agency had sent a text template. `message_delivery.variables_enc` is sealed under the `mail`
DEK (`app.messaging.compose`, `app.workflows.engine`), but `app.portability.tables.MAIL_TABLES` did not list
`message_delivery`, so the builder opened it under `vault` -> AES-GCM `integrity_check_failed`. `ops_release.payload_enc`
(sealed under the `ops_release` DEK, `app.ops_release.service`) had the same latent hole. The same purpose map also
serves the offboarding export (`app.lifecycle.offboarding.build_export` calls `builder.table_rows`), so an agency
offboarding with a text template or an ops release would have failed its export too: fixed by the same change.

## Decisions

- **Map, do not guess.** `message_delivery` joins `MAIL_TABLES`; `("ops_release", "payload_enc")` joins
  `COLUMN_PURPOSES` (`ops_release`). No fallback that tries several purposes: a value must open under the one purpose
  its writer used, or the export stops.
- **Guard by scanning the writers (AST), not a hand list.**
  `tests/unit/test_portability.py::test_every_sealed_column_opens_under_the_purpose_its_writer_used` walks every
  `.encrypt` / `.encrypt_bytes` call in `app/` with four positional arguments (the vault signature; the AES-GCM
  primitives take three), resolves table, column and `purpose=` from literals, module constants and imported
  constants, and checks `Table(t).purpose(column)` equals it. A seal whose purpose cannot be resolved fails the test
  (pass a literal or a constant). A column chosen at run time (`f"{field}_enc"`) is checked against the table's default
  purpose. Objects (`SEALED_OBJECTS`), never-exported tables and columns are skipped. Before the fix it listed exactly
  the 4 writers (compose 281, workflows engine 340, ops_release service 278 / 292).
- **Fail-closed stays.** The owner's export (and the pilot backup) still fails on a value that does not open; nothing
  is skipped and reported. What changed is what the failed row says: `builder.ValueDenied` (a `DecryptionDenied`, code
  `decrypt_denied`, 403) carries the table and column, and `crm.export_job.failure_class` becomes
  `decrypt_denied:<table>:<column>` (the column's CHECK allows `^[A-Za-z][A-Za-z0-9_:]{0,63}$`; the value is cleaned
  and cut to 64). The documents' content (`document:content`) is wrapped the same way.
- **The crm.job row keeps the identifier code.** `PermanentError(exc.code)` stays `decrypt_denied`: the worker stores
  `PermanentError: <code>` only when the code is an identifier (`_err`), so the colon form would be dropped there. The
  audit `export_create denied` keeps `decrypt_denied` too (`audit_event_reason_chk` allows no colon); the table and
  column live on the export row and in the `portability_export_failed` log line.
- **A failed pilot backup pages AA ops** through the existing in-app ops checks (`app.observability.ops_alerts`, the
  S263 alert diet path that e-mails the platform alerts SNS topic): new event check `pilot_backup_failed`, emitted by
  `exports._fail` when the failed row's `requested_by` is `job:crm_pilot_backup` (`FAILED_SQL ... RETURNING
  requested_by`). Scope = the export id, so one e-mail per failed export (a nightly backup is a new export each night,
  so a broken backup mails once a night). Details: tenant id, export id, failure code; never a value. An owner's own
  export failing does not page AA ops (the owner sees it on Settings > Data and privacy).
- **Every failure path of the job is covered**: a coded refusal (`decrypt_denied:...`, `export_not_configured` without
  a bucket) and the last attempt of an unexpected error (`export_error`) all go through `_fail`.

## Changed

- `api/app/portability/tables.py`: `MAIL_TABLES` + `message_delivery`; `COLUMN_PURPOSES` + `ops_release.payload_enc`.
- `api/app/portability/builder.py`: `ValueDenied` (+ `failure_class`); `table_rows` and the document read wrap
  `DecryptionDenied`; `_Zip.discard()` and a `try/except` around the build so a half-built ZIP is closed (and freed) at
  once instead of left to the garbage collector (it raised "I/O operation on closed file" warnings in `__del__`). The
  diff of `build()` is mostly that re-indent (`git diff -w` shows the real change).
- `api/app/portability/exports.py`: `failure_class(exc)`, `_fail` returns the requester, logs the failure, emits the
  ops alert for the pilot backup; module docstring.
- `api/app/observability/ops_alerts.py`: check `pilot_backup_failed` (title + action text) in `CHECKS` and
  `EVENT_CHECKS` (the registry test requires it to be emitted somewhere: `exports._fail`).
- Tests: unit (guard scan, `ValueDenied` naming, the job's failure paths + alert per requester), integration
  (`test_aa969_pilot.py`: an agency with a sent text template and an AA ops release, written by their real writers,
  backs up, the restore drill restores both tables with restored = manifest = 1 and the ZIP holds the opened values;
  then a value re-sealed under another DEK fails the next backup `decrypt_denied:message_delivery:variables_enc` and
  raises one `pilot_backup_failed`; the drill still finds the last good night).

## Tradeoffs

- **Skip-and-report vs fail-closed**: a skip would keep the nightly backup going but hand out a backup (or an owner's
  export) that silently lacks data; the lane brief and the portability design keep fail-closed. The alert makes the
  failure loud instead.
- **No migration**: `failure_class` already allows colons, so the table and column fit the existing column; no new
  `error` column (the brief's "export_job.error" maps onto `failure_class`, which the Settings page already shows as
  "The export failed ({code})").
- **No UI change**: the code shows verbatim on Settings > Data and privacy; `decrypt_denied:message_delivery:...` is a
  support code, acceptable there. Mapping it to friendly copy would need a new i18n string per table; not worth it for a
  state that should never recur after the guard.

## Should know

- The live exports of the pilot agency that failed before this fix stay `failed` (receipts); after deploy the next nightly backup
  (or an owner export) builds a new one. The orchestrator's live step re-runs the export and the restore drill.
- `pilot_backup_failed` e-mails only when `ALERTS_TOPIC_ARN` is set on the CRM API task (Dev has it since S263);
  locally it is logged `ops_check_finding`.
- The guard does not see a seal written through an alias (`seal = vault.encrypt`) or with a table name built at run
  time (it fails on the latter, by design). Keep vault seals literal.
