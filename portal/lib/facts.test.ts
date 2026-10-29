// portal/lib/facts.test.ts — AA-814: the Brand › Facts page rules (node --test, `npm test`).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EMPTY_FORM, changedFields, createBody, errorText, formOf, formProblem, overrideForm, ownOnSubject, subjectKey,
  type TenantFact,
} from "./facts.ts";

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
    { kind: "other", title: "a b", body: "c", provenance: "d", country: null, stated_on: null, valid_until: null });
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
