-- migrate: expand
-- Migration crm-029: AA-953 CRM-24a Module 11 portal per person (design v3 M11, R22, R33; data-privacy-integrations
-- §2.3 group B `traveler_flight`) — the structured arrival and departure flights a traveller (or their agent) gives in
-- Stage 3, so the itinerary shows the landing day in local time (an overnight flight lands on the next day) and the
-- ops hand-off (CRM-26) and the T-48 h "confirm arrival flight" task (R44 step 1) can read them without free text.
--
-- Owner: AA-Partner (api/migrations/029_crm_traveler_flight.sql). AA-CIS-Infra db/migrations/crm/ holds the byte copy
-- DB Migrate applies (Migration Copy Guard). Key crm-029 (crm-028 is claimed by a parallel lane; renumber at merge
-- if main has moved: file name, this header, the registration below, app/db.py REQUIRED_SCHEMA_VERSIONS).
--
-- What (DM-7: crm.traveler_flight already has its row in root data standards Part B §7, the F1 vault group: kept until
-- the agency deletes the traveller or offboards):
--   1. crm.traveler_flight (base template): one flight per (trip, traveller, direction arrival | departure):
--      flight_no_enc (the flight number under the tenant vault DEK, AAD = traveler_flight id; pii:travel), the airport
--      (IATA code: the landing airport of an arrival, the take-off airport of a departure), sched_local (the scheduled
--      local time AT that airport, without a zone) and tz (the airport's IANA zone, the proposal_day_tz_chk shape of
--      crm-019), who entered it (traveler | member). One live row per (trip, traveller, direction).
--   2. Erasure without a replaced crm.erase_subject: every foreign key of the table cascades. A trip erase deletes its
--      trip_traveler and trip rows, a traveller erase their trip_traveler and traveler rows, an agency erase every
--      trip and traveller, so the flights leave with them, and the agency erase's "nothing left" check finds none.
--      The hold guard below refuses the cascade on a held trip or traveller (crm.erase_held refuses the erase first).
--      tests/integration/test_crm010_schema.py lists the table as cascade-erased and checks the three ON DELETE
--      CASCADE keys, so a later key that replaces crm.erase_subject may name it but never has to.
--   3. RLS (ENABLE + FORCE, tenant_isolation TO the CRM roles); crm_app gets the default DML of crm-001; no reader
--      grant (AI, insights and support never see a flight, design §5.2); the updated_at trigger; the hold guard
--      (crm.guard_held_change on trip_id, traveler_id).
--
-- Class: expand (root ADR 0017 d3): one new table with its own constraints, indexes, triggers, policy and comments,
-- and foreign keys from it (the RI triggers they add on crm.trip, crm.traveler and crm.trip_traveler are allowed); no
-- change to any existing object, no DO block. The code PR that reads it carries this file and raises
-- REQUIRED_SCHEMA_VERSIONS to crm-029 (expand: one PR). The auto-apply stops at the first unregistered file that is not
-- expand, so a contract crm-028 is applied by hand first.
--
-- Rollback (a forward crm- file, after the code that reads it is reverted): DROP TABLE crm.traveler_flight.
--
-- Runs as crm_owner (DB Migrate switch), one BEGIN … COMMIT block, no role DDL.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS crm.traveler_flight (
    traveler_flight_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES crm.tenant (tenant_id),
    trip_id             UUID NOT NULL,
    traveler_id         UUID NOT NULL,
    direction           TEXT NOT NULL,
    flight_no_enc       BYTEA NOT NULL,
    airport_iata        TEXT NOT NULL,
    sched_local         TIMESTAMP NOT NULL,
    tz                  TEXT NOT NULL,
    entered_by_kind     TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          TEXT NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.actor', true), ''), current_user::text),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    deleted_by          TEXT,
    purge_after         TIMESTAMPTZ,
    CONSTRAINT traveler_flight_tenant_id_uq UNIQUE (tenant_id, traveler_flight_id),
    CONSTRAINT traveler_flight_trip_fk FOREIGN KEY (tenant_id, trip_id)
        REFERENCES crm.trip (tenant_id, trip_id) ON DELETE CASCADE,
    CONSTRAINT traveler_flight_traveler_fk FOREIGN KEY (tenant_id, traveler_id)
        REFERENCES crm.traveler (tenant_id, traveler_id) ON DELETE CASCADE,
    CONSTRAINT traveler_flight_party_fk FOREIGN KEY (trip_id, traveler_id)
        REFERENCES crm.trip_traveler (trip_id, traveler_id) ON DELETE CASCADE,
    CONSTRAINT traveler_flight_direction_chk CHECK (direction IN ('arrival', 'departure')),
    CONSTRAINT traveler_flight_enc_chk CHECK (octet_length(flight_no_enc) >= 30),
    CONSTRAINT traveler_flight_airport_chk CHECK (airport_iata ~ '^[A-Z]{3}$'),
    -- crm-019 proposal_day_tz_chk: an IANA zone name (the API checks it against the zone database too).
    CONSTRAINT traveler_flight_tz_chk CHECK (tz = 'UTC' OR tz ~ '^[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2}$'),
    CONSTRAINT traveler_flight_sched_chk CHECK (
        sched_local >= TIMESTAMP '2000-01-01 00:00' AND sched_local < TIMESTAMP '2100-01-01 00:00'
    ),
    CONSTRAINT traveler_flight_entered_by_chk CHECK (entered_by_kind IN ('traveler', 'member')),
    CONSTRAINT traveler_flight_trash_chk CHECK (
        (deleted_at IS NULL) = (deleted_by IS NULL) AND (deleted_at IS NULL) = (purge_after IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS traveler_flight_live_uq ON crm.traveler_flight (tenant_id, trip_id, traveler_id, direction)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS traveler_flight_traveler_idx ON crm.traveler_flight (traveler_id);
-- The ops hand-off and the T-48 h task read the arrivals of a day.
CREATE INDEX IF NOT EXISTS traveler_flight_sched_idx ON crm.traveler_flight (tenant_id, direction, sched_local)
    WHERE deleted_at IS NULL;

ALTER TABLE crm.traveler_flight ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm.traveler_flight FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON crm.traveler_flight
    TO crm_owner, crm_app, crm_ai_reader, crm_insights, crm_support_reader, crm_eraser
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE TRIGGER traveler_flight_set_updated_at
    BEFORE UPDATE ON crm.traveler_flight
    FOR EACH ROW EXECUTE FUNCTION crm.set_updated_at();
CREATE TRIGGER traveler_flight_hold_guard
    BEFORE DELETE OR UPDATE ON crm.traveler_flight
    FOR EACH ROW EXECUTE FUNCTION crm.guard_held_change('trip_id', 'traveler_id');

COMMENT ON TABLE crm.traveler_flight IS 'AA-953 (crm-029; design v3 R33, R44, data-privacy-integrations §2.3 group B): the arrival and departure flight of one traveller on one trip, written in the client portal (Stage 3) or by the agent. The flight number is under the tenant vault DEK; the airport, the scheduled local time and its IANA zone are plain so the ops hand-off and the T-48 h task can query them. One live row per trip, traveller and direction. Every foreign key cascades: the flights leave with their trip, traveller or party row (erasure included). No reader role is granted it.';
COMMENT ON COLUMN crm.traveler_flight.flight_no_enc IS 'pii:travel; the flight number (carrier code + number, e.g. VN611), AES-256-GCM under the tenant vault DEK, AAD = tenant, traveler_flight, flight_no_enc, id';
COMMENT ON COLUMN crm.traveler_flight.airport_iata IS 'pii:travel; the IATA code of the landing airport (arrival) or the take-off airport (departure)';
COMMENT ON COLUMN crm.traveler_flight.sched_local IS 'pii:travel; the scheduled local time at airport_iata, without a zone (the zone is tz): an overnight flight lands on its own local date';
COMMENT ON COLUMN crm.traveler_flight.tz IS 'the IANA zone of airport_iata (sched_local AT TIME ZONE tz is the instant)';
COMMENT ON COLUMN crm.traveler_flight.entered_by_kind IS 'traveler (the client portal) | member (an agent)';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('crm-029', now(), 'AA-953 CRM-24a portal per person: crm.traveler_flight (structured arrival / departure flights per trip and traveller, flight number under the vault DEK, local time + IANA zone; cascading keys, no erase_subject change)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
