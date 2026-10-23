-- Base table template for every tenant table in schema crm (design v3 §3.2, Fix B6; AA-933 / CRM-05).
--
-- No runner applies this file. A card that adds a tenant table copies the block below into its own crm-NNN migration
-- and renames {{table}} / {{key}}. The integration test tests/integration/test_crm002_tenancy.py
-- (test_every_crm_table_follows_the_base_template) reads pg_catalog and fails when a table in schema crm misses any
-- part of it, unless the table is listed in PLATFORM_TABLES there with a reason (crm.job, crm.status_transition).
--
-- The parts:
--   * base columns: tenant_id (default = the transaction's app.tenant_id, FK to crm.tenant), created_at, created_by
--     (default = app.actor, set by tenant_tx() to "member:<id>", else the database role), updated_at;
--   * trash columns from the first migration (R21, Fix B6): deleted_at, deleted_by, purge_after, set together or not
--     at all ({{table}}_trash_chk). CRM-14 adds only the behaviour (trash view, restore, purge job);
--   * ENABLE + FORCE row level security and the one tenant policy TO the six CRM roles (crm_owner, crm_app, the four
--     readers; never TO PUBLIC, catalogue rule b): no tenant set = no row;
--   * the updated_at trigger;
--   * a status column, when the table has one: TEXT + CHECK, plus rows in crm.status_transition and the trigger;
--   * personal data: every PII column gets COMMENT 'pii:<class>' (design v3 §3.2); encrypted columns are *_enc,
--     blind indexes *_bidx (CRM-06);
--   * money rows: amount numeric(14,2), currency char(3) and the frozen FX block (design v3 §3.2), corrections as new
--     rows with supersedes_id.
-- Append-only variant (crm-003, crm.audit_event): base columns tenant_id, created_at, created_by only (no updated_at,
-- no trash columns), the same RLS policy, the trigger crm.reject_append_only_change() BEFORE UPDATE OR DELETE (and
-- BEFORE TRUNCATE), and REVOKE UPDATE, DELETE, TRUNCATE FROM crm_app; listed in APPEND_ONLY_TABLES of the test.
-- Grants: none to write. crm-001's default privileges give crm_app DML on every new table; the NOLOGIN readers get
-- nothing by default (grant them table by table, never on the F1 vault tables).

CREATE TABLE IF NOT EXISTS crm.{{table}} (
    {{key}}      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                 REFERENCES crm.tenant (tenant_id),
    -- … the table's own columns …
    status       TEXT NOT NULL DEFAULT '<first status>',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   TEXT NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), current_user::text),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ,
    deleted_by   TEXT,
    purge_after  TIMESTAMPTZ,
    CONSTRAINT {{table}}_status_chk CHECK (status IN ('<first status>', '<next status>')),
    CONSTRAINT {{table}}_trash_chk CHECK (
        (deleted_at IS NULL) = (deleted_by IS NULL) AND (deleted_at IS NULL) = (purge_after IS NULL)
    )
);

ALTER TABLE crm.{{table}} ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm.{{table}} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON crm.{{table}};
CREATE POLICY tenant_isolation ON crm.{{table}}
    TO crm_owner, crm_app, crm_ai_reader, crm_insights, crm_support_reader, crm_eraser
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE OR REPLACE TRIGGER {{table}}_set_updated_at
    BEFORE UPDATE ON crm.{{table}}
    FOR EACH ROW EXECUTE FUNCTION crm.set_updated_at();

-- Only for a table with a status column:
INSERT INTO crm.status_transition (table_name, from_status, to_status) VALUES
    ('{{table}}', '<first status>', '<next status>')
ON CONFLICT DO NOTHING;
CREATE OR REPLACE TRIGGER {{table}}_status_transition
    BEFORE UPDATE OF status ON crm.{{table}}
    FOR EACH ROW EXECUTE FUNCTION crm.enforce_status_transition();
