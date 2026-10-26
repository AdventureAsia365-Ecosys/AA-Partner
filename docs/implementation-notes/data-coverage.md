# Data coverage (AA-992) — CRM part: crm.ops.v1 1.4 `GET /crm/ops/v1/data-coverage`

The CIS admin page `/admin/data-coverage` (S252 design `.tmp-session/s252/datacov/design.md` §1.9, §3.4) shows CRM
counts. ADR 0008a decision 1 forbids any AA role USAGE on schema `crm` (the isolation probe asserts 42501 both ways),
so the numbers come over HTTP, one more read of the L1 contract `crm.ops.v1`, minor 1.4 (additive).

## Decisions

- **Minor 1.4, not 1.3 (fix round 2).** AA-979 P2b shipped `POST /tenants/{id}/studio-invitations` as
  crm.ops.v1 1.3 (root #267, this repo #74) while this branch was open; the branch was rebased on it and the data
  coverage read renumbered 1.4.

- **No crm migration.** `crm.tenant` / `crm.member` are FORCE RLS; the agencies come from the crm-007 registry
  (`crm.audit_export_agencies()`) and each one is counted inside its own `platform_tx`, exactly like `list_agencies`.
  `crm.aa_catalog_tour` / `aa_catalog_state` are platform tables read on the pool (`app/catalog/store.py`, already a
  DB module). The AA-979 lane owns crm-020 and AA-950 may take crm-021, so nothing here touches `api/migrations/`.
- **Audit (critic B12 / Q3): no per-agency `ops_read` event.** The route reads no agency's data, only statuses and
  member statuses, and the CIS page refreshes it every 10 minutes; one event per agency per refresh would bury the
  owner's access log. There is no platform-level audit table, so the read is logged once per call
  (`crm_ops_data_coverage agencies=<n>`), the same as `/jobs/summary` (no event either). The 1.4 contract text says so.
- **Response** `{as_of, agencies{active,locked,offboarding,offboarded,synthetic}, members{active,invited,disabled},
  catalog_cache{served,paused,retired,last_full_pass_at,last_ok_at,failures,last_error_class}}`; `synthetic` counts
  synthetic agencies of any status (also counted under their status). No trip, proposal, traveller, revenue or id.
- **Auth**: route class OPS read (`Reader`: `cis-admin` or `crm-platform-admin`), like every other GET.

## Changed

- `api/app/routers/ops.py`: models `AgencyCounts`, `MemberCounts`, `CatalogCacheCounts`, `DataCoverage`
  (`extra='forbid'`), route `GET /data-coverage`.
- `api/app/ops/console.py`: `platform_coverage()` (+ module docstring: the one read without an agency event).
- `api/app/catalog/store.py`: `COVERAGE_SQL`, `coverage()`.
- `api/app/ops/serializer.py`: 17 allow-listed keys (none matches `OPS_FORBIDDEN`).
- Tests: `test_ops_contract.py` scenario (every served ops route must be exercised) + scripted rows;
  `test_ops_console.py::test_platform_coverage_counts_states_and_members_without_an_agency_audit`.
- `CONTEXT.md`: "CIS sees the CRM" bullet.
- Root PR (same day): `docs/contracts/crm.ops.v1.openapi.yaml` 1.3 → 1.4 + samples `samples/crm.ops.v1/data-coverage-*`.

## Tradeoffs

- N platform transactions per call (one per agency; 4 agencies on Dev). Fine at L1 scale; if agencies grow into the
  hundreds, a crm SECURITY DEFINER count function in a later crm-NNN is the cheaper path.
- `last_error_class` is the sync's own error code (`^[a-z][a-z0-9_]{0,59}$` CHECK), not free text.

## Should know

- Unit suite: 1704 passed after the rebase on #74 (fix round 2; 1674 before); ruff check + format clean. No integration test added (no new SQL object; the SQL reads
  columns that the crm-017 / crm-001 integration tests already cover). Run `GET /crm/ops/v1/data-coverage` against
  Dev after deploy with a `cis-admin` token (CIS page does it).
