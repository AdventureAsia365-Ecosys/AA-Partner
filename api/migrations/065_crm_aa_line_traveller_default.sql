-- Migration crm-065: AA-1086 follow-up (S279 rehearsal REH-10-portal) — a confirmed Adventure Asia line is shown to the
-- trip's travellers by default: the confirmed AA lines written before the code that sets the default get it once.
--
-- Owner: AA-Partner (api/migrations/065_crm_aa_line_traveller_default.sql). AA-CIS-Infra db/migrations/crm/ holds the
-- byte copy Migration Apply runs (Migration Copy Guard). Key crm-065 = crm main max + 1 (crm-064 is the highest); if
-- main moves, renumber: file name, this header, Guard 0 and the registration below, app/db.py
-- REQUIRED_SCHEMA_VERSIONS and the tests that name it (git grep '065_' and 'crm-065').
--
-- Why: the traveller portal lists only the trip's lines the agency shows to travellers
-- (crm.booking_line.visible_to_travelers, AA-953 R22), and a traveller voucher is released only for such a line
-- (AA-1070 crm-20). An AA line is read-only for members (crm-014 guard 3a, CRM24: only the Booking feed job writes
-- it), so the agency could never show its confirmed AA tour to its travellers: the S279 rehearsal portal listed the
-- trip's days but no service. Tech lead decision (13/11/2026): a confirmed AA line is visible to travellers by default.
-- The AA-Partner code sets the flag when the Booking sync confirms an AA line (app/booking/sync.py BOOKED_SQL and
-- CONFIRM_LINE_SQL: on the move to confirmed only, so a later choice is kept); this file gives the same default to the
-- AA lines confirmed before that code.
--
-- What: one data backfill, no DDL. Every agency is entered through app.tenant_id (FORCE RLS hides crm.booking_line from
-- the owner; the agency ids come from the crm-007 export registry, as crm-014 section 2 does) and its confirmed,
-- live AA lines that are not shown get visible_to_travelers = true. A cancelled line stays hidden (the guard
-- crm.guard_booking_line clears the flag on a cancelled line; the CHECK booking_line_cancelled_hidden_chk refuses it).
-- Runs once: the backfill is skipped when crm-065 is already registered (a re-run never shows a line again).
--
-- Behaviour on apply: the confirmed AA lines of every agency appear in their trip's traveller portal (title and dates,
-- never a price) and their vouchers are released to the travellers once the trip is booked. Not personal data.
--
-- Class: contract (writes rows of an existing table). MIG-4: this migration-only PR, then the code PR. No new table
-- (no DM-7.1 row), no crm.erase_subject change.
--
-- Rollback: none needed (a member-visible flag; a later choice to hide a line is the agency's, not a migration's).
--
-- Runs as crm_owner (DB Migrate switch), one BEGIN … COMMIT block, no role DDL. Re-runnable (the registration check
-- below, ON CONFLICT DO NOTHING).

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- ── Guard 0: runs as the owner, after crm-064 ────────────────────────────────────────────────────
DO $$
BEGIN
    IF current_user <> 'crm_owner' THEN
        RAISE EXCEPTION 'crm-065: must run as crm_owner (DB Migrate live-owner switch), runs as %', current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = 'crm-064') THEN
        RAISE EXCEPTION 'crm-065: crm-064 (AA-1096 settings registry) lands first';
    END IF;
    IF EXISTS (SELECT 1 FROM shared.schema_versions
                WHERE version = 'crm-065' AND description NOT LIKE 'AA-1086: confirmed AA lines shown to travellers%') THEN
        RAISE EXCEPTION 'crm-065: the key crm-065 is already held by another migration (not AA-1086): renumber one';
    END IF;
END $$;

-- ── 1. Backfill: confirmed AA lines shown to travellers, once ────────────────────────────────────
DO $$
DECLARE
    v_id uuid;
    v_count bigint := 0;
    v_rows bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = 'crm-065') THEN
        RAISE NOTICE 'crm-065: already applied, the backfill is not run again';
        RETURN;
    END IF;
    FOR v_id IN SELECT x.tenant_id FROM crm.audit_export_tenant x ORDER BY 1 LOOP
        PERFORM set_config('app.tenant_id', v_id::text, true);
        UPDATE crm.booking_line l SET visible_to_travelers = true
         WHERE l.supplier_kind = 'aa' AND l.status = 'confirmed' AND NOT l.visible_to_travelers
           AND l.deleted_at IS NULL;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_count := v_count + v_rows;
    END LOOP;
    PERFORM set_config('app.tenant_id', '', true);
    RAISE NOTICE 'crm-065: % confirmed AA line(s) now shown to travellers', v_count;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('crm-065', now(), 'AA-1086: confirmed AA lines shown to travellers by default (backfill of crm.booking_line.visible_to_travelers)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
