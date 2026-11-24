// web/tests/agencyItems.test.ts — AA-1144: the agency's own items and "Ask AA for extra nights" (vitest, `npm test`).
import assert from "node:assert/strict";
import { test } from "vitest";

import { AGENCY_ITEM_CATEGORIES, asksAaForNights, canBundle, drawerTabs, extraNightBody, type ExtraNightForm } from "@/lib/agencyItems";
import { CATEGORIES } from "@/lib/catalogue";
import { matchBff } from "@/lib/bff";
import { matchStaffBff } from "@/lib/staffBff";

const FORM: ExtraNightForm = {
  position: "after", place: "  Lantern   Bay ", check_in: "2027-03-08", nights: "2", rooms: "1", adults: "2", children: "",
};

test("the closed list is flight, visa, travel insurance and the agency fee, in the catalogue too", () => {
  assert.deepEqual([...AGENCY_ITEM_CATEGORIES], ["flight", "visa", "insurance", "fee"]);
  assert.deepEqual([...CATEGORIES], [...AGENCY_ITEM_CATEGORIES]);
});

test("the drawer offers agency items on an Other block only; a lodging block asks AA for nights", () => {
  assert.deepEqual(drawerTabs("other"), ["aa", "addon", "agency", "catalogue", "ai"]);
  for (const kind of ["lodging", "activity", "transport", "meal", "guide"] as const) {
    assert.deepEqual(drawerTabs(kind), ["aa", "addon", "ai"], kind);
  }
  assert.equal(asksAaForNights("lodging"), true);
  assert.equal(asksAaForNights("activity"), false);
});

test("the extra night request body", () => {
  assert.deepEqual(extraNightBody(FORM), {
    body: { position: "after", place: "Lantern Bay", check_in: "2027-03-08", nights: 2, rooms: 1, adults: 2, children: 0 },
  });
  const bad: [Partial<ExtraNightForm>, keyof ExtraNightForm][] = [
    [{ position: "during" }, "position"],
    [{ place: " " }, "place"],
    [{ place: "x".repeat(81) }, "place"],
    [{ check_in: "" }, "check_in"],
    [{ nights: "0" }, "nights"],
    [{ nights: "61" }, "nights"],
    [{ nights: "1.5" }, "nights"],
    [{ rooms: "31" }, "rooms"],
    [{ adults: "" }, "adults"],
    [{ children: "-1" }, "children"],
  ];
  for (const [change, field] of bad) assert.deepEqual(extraNightBody({ ...FORM, ...change }), { invalid: field });
});

test("the BFFs pass the new routes only", () => {
  const id = "0b8f5a2e-6f0e-4c35-9a4c-6e1d2b3c4d5e";
  assert.ok(matchBff("GET", `trips/${id}/aa-extra-nights`));
  assert.ok(matchBff("POST", `trips/${id}/aa-extra-nights`));
  assert.ok(matchBff("POST", `aa-extra-nights/${id}/close`));
  assert.equal(matchBff("DELETE", `aa-extra-nights/${id}`), null);
  assert.ok(matchStaffBff("GET", "aa-extra-nights"));
  assert.equal(matchStaffBff("POST", "aa-extra-nights"), null);
});

test("AA-1147 (T18): only a flight or the agency fee may be folded into the tour price", () => {
  assert.deepEqual(AGENCY_ITEM_CATEGORIES.filter(canBundle), ["flight", "fee"]);
  assert.equal(canBundle("visa"), false);
  assert.equal(canBundle("insurance"), false);
  assert.equal(canBundle(null), false);
});
