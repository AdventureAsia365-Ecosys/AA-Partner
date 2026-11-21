// web/tests/agencyExtras.test.ts — AA-1147: the agency's own services follow the trip (vitest, `npm test`).
import assert from "node:assert/strict";
import { test } from "vitest";

import { bundledValue } from "@/lib/agencyItems";
import { lineFor, lineTitle } from "@/lib/clients";
import { newLineBody, serviceLines, type Line, type TripLines } from "@/lib/ledger";

function form(fields: Record<string, string>): FormData {
  const f = new FormData();
  for (const [k, v] of Object.entries(fields)) f.set(k, v);
  return f;
}

test("Add line: no supplier is handled by the agency, the deadline and the fold travel with it", () => {
  const body = newLineBody(form({ agency_supplier_id: "", category: "flight", title: " Return flight ", due_on: "2031-02-01", bundled: "on" }));
  assert.equal(body.agency_supplier_id, null);
  assert.equal(body.title, "Return flight");
  assert.equal(body.due_at, "2031-02-01");
  assert.equal(body.bundled, true);
  assert.equal(body.collect, "agency");
  const own = newLineBody(form({ agency_supplier_id: "s-1", category: "visa", title: "E-visa" }));
  assert.equal(own.agency_supplier_id, "s-1");
  assert.equal(own.due_at, null);
  assert.equal(own.bundled, false);
});

test("only a flight or the fee is sent folded into the tour price", () => {
  assert.equal(bundledValue(true, "flight"), true);
  assert.equal(bundledValue(true, "fee"), true);
  assert.equal(bundledValue(true, "visa"), false);
  assert.equal(bundledValue(false, "flight"), false);
});

test("a trip file names one of its agency service lines", () => {
  const lines = [
    { booking_line_id: "l-aa", supplier_kind: "aa", title: "Lantern bay cruise" },
    { booking_line_id: "l-1", supplier_kind: "agency", title: "Return flight" },
  ] as unknown as Line[];
  const view = { trip_id: "t-1", lines } as unknown as TripLines;
  assert.deepEqual(serviceLines({ ok: true, data: view }), [{ booking_line_id: "l-1", title: "Return flight" }]);
  assert.deepEqual(serviceLines({ ok: false }), []);
  assert.equal(lineFor("t-1", "l-1"), "l-1");
  assert.equal(lineFor("t-1", ""), null);
  assert.equal(lineFor(undefined, "l-1"), null);
  assert.equal(lineTitle([{ booking_line_id: "l-1", title: "Return flight" }], "l-1"), "Return flight");
  assert.equal(lineTitle([], null), null);
});
