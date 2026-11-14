# AA-1093 stage 2 (S281 lane l2-erase-crm) — crm-066 / crm-067, the storage of the CRM erasure step

The migration-only PR (MIG-4) before the code PR of the same lane; the decisions are in
`docs/implementation-notes/AA-1093-l2-erase-crm.md` (code PR).

## Decisions

- **crm-066 is contract** (S281: the expand post-check refuses a new crm table without RLS): three new platform tables (no tenant column: a CIS tenant may have no agency, and the record
  outlives the agency erase), their constraints, indexes, `set_updated_at` triggers and comments; no row written (the
  feed state's singleton row is inserted by the job on first use, so the file stays expand).
- **The report's status column is `outcome`**: a `status` column needs `crm.status_transition` rows (crm-002 base
  template), an INSERT into an existing table.
- **crm-067 is contract**: a SECURITY DEFINER function (REVOKE / GRANT, a DO post-check), not a DELETE grant on
  `crm.studio_invitation` (the crm-020 row policy also lets `job:crm_studio_activation` and `job:crm_partner_accounts`
  through: a grant would let them delete). Only `job:crm_tenant_erase` may call it (CRM56, a new code).

## Changed

- Nothing else in the schema: `crm.erase_subject` is unchanged (the new tables have no tenant column, so its CRM16
  check never meets them).

## Tradeoffs

- crm-067 waits for a Migration Apply (contract) while crm-066 could be auto-applied: one batch, applied in order.

## Should know

- Batch crm 066-067 (one copy, one Migration Apply). Retention rows (DM-7.1): the root PR of this lane.
- Tests: `api/tests/integration/test_crm066_schema.py`; `test_crm002_tenancy.py` lists the three platform tables.
