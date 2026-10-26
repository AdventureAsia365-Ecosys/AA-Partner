// AA-990 scope 5: the "Adventure Asia updated this tour" notice rules (lib/masterNotice.ts). Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import { MASTER_NEWER_LABEL, masterNoticeText, showMasterNotice } from "./masterNotice.ts";

const fmt = (iso: string) => (iso.startsWith("2026-10-24") ? "24 Oct 2026" : iso);

test("master_newer true: the full sentence with the date", () => {
  assert.equal(
    masterNoticeText({ master_newer: true, master_updated_at: "2026-10-24T08:00:00Z" }, fmt),
    "Adventure Asia updated this tour on 24 Oct 2026, after your version. Your text was not changed. "
      + "Rewrite it if you want the new details.",
  );
});

test("no date (or an empty formatted date): the sentence without 'on <date>'", () => {
  const expected = "Adventure Asia updated this tour, after your version. Your text was not changed. "
    + "Rewrite it if you want the new details.";
  assert.equal(masterNoticeText({ master_newer: true, master_updated_at: null }, fmt), expected);
  assert.equal(masterNoticeText({ master_newer: true, master_updated_at: "2026-10-24T08:00:00Z" }, () => "—"), expected);
});

test("master_newer false, null or absent (CIS not deployed yet): nothing", () => {
  for (const v of [{ master_newer: false }, { master_newer: null }, {}, null, undefined]) {
    assert.equal(showMasterNotice(v), false);
    assert.equal(masterNoticeText(v, fmt), null);
  }
});

test("only a real true counts (a truthy non-boolean from a bad payload shows nothing)", () => {
  assert.equal(showMasterNotice({ master_newer: "true" as unknown as boolean }), false);
});

test("a paused or retired tour keeps its own served badge: no rewrite invitation", () => {
  for (const served_status of ["paused", "retired"] as const) {
    assert.equal(showMasterNotice({ master_newer: true, served_status }), false);
    assert.equal(masterNoticeText({ master_newer: true, served_status }, fmt), null);
  }
  assert.equal(showMasterNotice({ master_newer: true, served_status: "served" }), true);
});

test("the copy is content-only: no gate, score or QA words", () => {
  const text = masterNoticeText({ master_newer: true, master_updated_at: "2026-10-24T08:00:00Z" }, fmt) ?? "";
  for (const s of [text, MASTER_NEWER_LABEL]) {
    assert.doesNotMatch(s, /score|quality|qa|gate|review|flag/i);
  }
});
