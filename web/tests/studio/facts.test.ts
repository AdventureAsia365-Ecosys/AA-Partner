// web/tests/studio/facts.test.ts — AA-814: the Brand › Facts page rules (vitest, `npm test`).
import assert from "node:assert/strict";
import { test } from "vitest";

import {
  EMPTY_FORM, FACT_KIND_REFUSAL, agencyMayWrite, changedFields, createBody, errorText, factReadOnly, factWriteRefusal,
  formKinds,
  formOf, formProblem, overrideForm, ownOnSubject, subjectKey,
  type TenantFact,
} from "@/lib/studio/facts";

const OWN: TenantFact = {
  fact_id: "fact_own1", scope: "tenant", kind: "price", title: "Mekong cruise price", body: "From USD 900.",
  provenance: "Our 2027 rate sheet", country: "Laos", stated_on: "2026-10-01", valid_until: null,
  created_at: null, updated_at: "2026-10-01T08:00:00+00:00", expired: false,
};
const PLATFORM: TenantFact = { ...OWN, fact_id: "fact_aa1", scope: "platform", body: "From USD 1000.",
  provenance: "Adventure Asia", overridden_by: null };

test("the subject key ignores case and spacing, not kind or country", () => {
  assert.equal(subjectKey(OWN), subjectKey({ kind: "price", title: " MEKONG  cruise price", country: "laos" }));
  assert.notEqual(subjectKey(OWN), subjectKey({ ...OWN, kind: "season" }));
  assert.notEqual(subjectKey(OWN), subjectKey({ ...OWN, country: null }));
});

test("Use my own copies the subject only", () => {
  assert.deepEqual(overrideForm(PLATFORM), { ...EMPTY_FORM, kind: "price", title: "Mekong cruise price",
    country: "Laos" });
});

test("an own fact on the same subject is found (not the one being edited)", () => {
  assert.equal(ownOnSubject([OWN], overrideForm(PLATFORM))?.fact_id, "fact_own1");
  assert.equal(ownOnSubject([OWN], formOf(OWN), "fact_own1"), undefined);
  assert.equal(ownOnSubject([OWN], { ...overrideForm(PLATFORM), kind: "season" }), undefined);
});

test("create body and changed fields", () => {
  assert.deepEqual(createBody({ ...EMPTY_FORM, title: " a  b ", body: "c", provenance: "d" }),
    { kind: "booking_terms", title: "a b", body: "c", provenance: "d", country: null, stated_on: null, valid_until: null });
  assert.deepEqual(changedFields(OWN, formOf(OWN)), {});
  assert.deepEqual(changedFields(OWN, { ...formOf(OWN), valid_until: "2027-03-31" }), { valid_until: "2027-03-31" });
});

test("form problems", () => {
  assert.equal(formProblem(EMPTY_FORM), "required");
  assert.equal(formProblem(formOf(OWN)), null);
  assert.equal(formProblem({ ...formOf(OWN), valid_until: "2026-09-01" }), "dates");
});

test("error text", () => {
  assert.match(errorText(403, "studio_role_forbidden"), /role can read/);
  assert.match(errorText(409, "x"), /changed since/);
  assert.equal(errorText(422, "an agency can keep at most 200 facts; delete one first"),
    "An agency can keep at most 200 facts; delete one first");
  assert.equal(errorText(422, [{ loc: ["body", "title"], msg: "too long" }]), "title: too long");
  assert.equal(errorText(0, null), "Network error");
});

test("AA-1144 (B6, D7): an agency writes its booking terms only; prices and tour facts stay Adventure Asia's", () => {
  assert.deepEqual(["booking_terms"].map(agencyMayWrite), [true]);
  assert.deepEqual(["price", "season", "visa_entry", "transfer_time", "other"].map(agencyMayWrite),
    [false, false, false, false, false]);
  assert.equal(EMPTY_FORM.kind, "booking_terms");
  assert.deepEqual(formKinds("booking_terms"), ["booking_terms"]);
  assert.deepEqual(formKinds("season"), ["booking_terms", "season"]); // an older own fact still shows its kind
  // An older own fact of another kind is read only (delete only); CIS also says so with read_only.
  assert.equal(factReadOnly({ kind: "price" }), true);
  assert.equal(factReadOnly({ kind: "booking_terms" }), false);
  assert.equal(factReadOnly({ kind: "booking_terms", read_only: true }), true);
});

test("AA-1144 (B6): the tenant proxy refuses a facts write of a tour kind before CIS", () => {
  const json = "application/json";
  const fact = (kind?: unknown) => JSON.stringify({ ...(kind === undefined ? {} : { kind }), title: "Ferry to Isla Verde",
    body: "Two hours.", provenance: "Our own trip notes" });
  const kindRefusal = { status: 422, detail: FACT_KIND_REFUSAL };
  // A new own fact: booking terms pass; a price, a tour kind, a missing kind (CIS would make it "other") refuse.
  assert.equal(factWriteRefusal("post", ["v1", "facts"], json, fact("booking_terms")), null);
  for (const kind of ["price", "season", "visa_entry", "transfer_time", "other", undefined, null, 3, ["price"]]) {
    assert.deepEqual(factWriteRefusal("POST", ["v1", "facts"], json, fact(kind)), kindRefusal, String(kind));
  }
  // An edit: setting a tour kind (or null) refuses; leaving the kind alone passes, also on an older fact.
  assert.deepEqual(factWriteRefusal("PATCH", ["v1", "facts", "fact_x1"], json, fact("transfer_time")), kindRefusal);
  assert.deepEqual(factWriteRefusal("PATCH", ["v1", "facts", "fact_x1"], json, JSON.stringify({ kind: null })),
    kindRefusal);
  assert.equal(factWriteRefusal("PATCH", ["v1", "facts", "fact_x1"], json, JSON.stringify({ body: "Three hours." })),
    null);
  assert.equal(factWriteRefusal("PATCH", ["v1", "facts", "fact_x1"], json, fact("booking_terms")), null);
  assert.deepEqual(factWriteRefusal("PATCH", ["v1", "facts", "fact_x1"], json, fact("price")), kindRefusal);
  // A body the check cannot read never passes.
  const notJson = { status: 400, detail: "A fact is sent as JSON." };
  assert.deepEqual(factWriteRefusal("POST", ["v1", "facts"], json, "{kind: season"), notJson);
  assert.deepEqual(factWriteRefusal("POST", ["v1", "facts"], json, undefined), notJson);
  assert.deepEqual(factWriteRefusal("POST", ["v1", "facts"], json, "[]"), notJson);
  assert.deepEqual(factWriteRefusal("PATCH", ["v1", "facts", "fact_x1"], "multipart/form-data; boundary=x", "--x"),
    notJson);
  // Other routes and methods are not this check's business.
  assert.equal(factWriteRefusal("DELETE", ["v1", "facts", "fact_x1"], json, undefined), null);
  assert.equal(factWriteRefusal("GET", ["v1", "facts"], json, undefined), null);
  assert.equal(factWriteRefusal("POST", ["v1", "brand"], json, fact("season")), null);
  assert.equal(factWriteRefusal("POST", ["v1", "facts", "fact_x1", "extra"], json, fact("season")), null);
});
