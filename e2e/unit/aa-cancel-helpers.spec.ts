// e2e/unit/aa-cancel-helpers.spec.ts — AA-1087 / AA-1070 crm-13: helpers/aaCancel.ts (no browser, no network).
import { expect, test } from "@playwright/test";
import { flaggedTripIds, readSeeded } from "../helpers/aaCancel";

const DEPARTURE = "4b8e2c1a-7d3f-4e9a-8b2c-1f0e9d8c7b6a";
const APPLY = {
  mode: "APPLY",
  fixtures: {
    run: "s278a",
    agency: { tenant_id: "t-1", partner_id: "p-1", name: "Invented Agency", crm_e2e: true },
    tour_source_id: "src-lantern-bay",
    departure_id: DEPARTURE,
    departure_start: "2027-01-11",
    tour_component_id: "tp:plan-1",
  },
};

test("readSeeded takes the departure, its start, the tour and the CRM flag", () => {
  expect(readSeeded(JSON.stringify(APPLY))).toEqual({
    departureId: DEPARTURE,
    startDate: "2027-01-11",
    tourSourceId: "src-lantern-bay",
    componentId: "tp:plan-1",
    crmAgency: true,
  });
  const other = { ...APPLY, fixtures: { ...APPLY.fixtures, agency: { crm_e2e: false } } };
  expect(readSeeded(JSON.stringify(other)).crmAgency).toBe(false);
});

test("readSeeded refuses what is not a complete APPLY output", () => {
  expect(() => readSeeded(JSON.stringify({ mode: "DRY RUN", plan: {} }))).toThrow(/APPLY output/);
  expect(() => readSeeded(JSON.stringify({ fixtures: { ...APPLY.fixtures, departure_id: "d-1" } }))).toThrow(/UUID/);
  expect(() => readSeeded(JSON.stringify({ fixtures: { ...APPLY.fixtures, departure_start: null } }))).toThrow(/date/);
  expect(() => readSeeded(JSON.stringify({ fixtures: { ...APPLY.fixtures, tour_component_id: "" } }))).toThrow(/missing/);
});

test("flaggedTripIds reads open stages and the Closed group", () => {
  const board = {
    stages: [{ cards: [{ trip_id: "a" }, { trip_id: "b" }] }, { cards: [] }, {}],
    closed: { cards: [{ trip_id: "c" }] },
  };
  expect(flaggedTripIds(board)).toEqual(["a", "b", "c"]);
  expect(flaggedTripIds({})).toEqual([]);
});
