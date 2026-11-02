# AA-1031 (S264 lane l10) — Create agency on a deleted agency's id: 409 `tenant_deleted`

Tech lead test (S264): a test agency was deleted (S263 "Delete test agency": the `crm.tenant` row goes to the trash,
`deleted_at` set, purged later), then "Create agency" for the same CIS tenant answered the opaque 409
`tenant_exists` ("An agency with this id already exists"), while no list shows that agency any more.

## Decisions

- New refusal code `tenant_deleted` (409) on `POST /crm/ops/v1/tenants`, only on the create-only path
  (`provision_agency(create_only=True)`, the L1 console). The seeder's converge path is unchanged.
- The check reads the existing row in the same provisioning transaction (`TENANT_TRASHED_SQL`), after the
  `INSERT ... ON CONFLICT DO NOTHING` returned no row. `app.tenant_id` is the agency id there, so the crm-002
  `tenant_isolation` policy shows the row whatever its `deleted_at` (no new grant, no migration).
- Message: "the agency with this id was deleted and its id cannot be reused; create a new CIS tenant". The CIS admin
  page words it in its own copy (CIS PR, `CRM_ERRORS.tenant_deleted`).

## Changed

- `api/app/agencies.py` (`TENANT_TRASHED_SQL`, the create-only branch), docstrings in `app/ops/console.py` and
  `app/routers/ops.py`, `CONTEXT.md` (L1 console writes).
- Root contract `crm.ops.v1` 1.9 (additive): the 409 of createTenant lists `tenant_deleted` (root PR of this lane).

## Tradeoffs

- Restoring the trashed agency instead was rejected: its keys are crypto-shredded at delete, so it can never be
  usable again; the honest answer is "make a new CIS tenant".
- One extra read only on the refusal path (an existing id); the happy path is unchanged.

## Should know

- Tests: unit `test_agencies.py`, `test_ops_console.py`, `test_ops_contract.py` (a 409 `tenant_deleted` row);
  integration `test_crm011_console.py::test_create_on_a_deleted_test_agency_id_is_409_tenant_deleted` (create,
  delete, create again).
