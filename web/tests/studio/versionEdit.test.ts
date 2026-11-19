// AA-1144 follow-up (S285 live check): a My Content save sends only the fields the agency edited. Run: npm test.
import { test } from "vitest";
import assert from "node:assert/strict";
import { editedFields, hasEdits } from "@/lib/studio/versionEdit";

test("a summary edit sends the summary only, never the Master's name, subtitle or SEO lines", () => {
  const sent = editedFields({ summary: "Walk Hollin Ridge with our team.", highlights: null });
  assert.deepEqual(sent, { summary: "Walk Hollin Ridge with our team." });
  for (const key of ["name", "subtitle", "seo_title", "seo_meta", "itineraries"]) assert.ok(!(key in sent), key);
});

test("a highlights edit sends the highlights only", () => {
  assert.deepEqual(editedFields({ summary: null, highlights: ["Sunrise from Hollin Ridge"] }), {
    highlights: ["Sunrise from Hollin Ridge"],
  });
});

test("both edits are sent; an emptied field is still an edit", () => {
  assert.deepEqual(editedFields({ summary: "", highlights: [] }), { summary: "", highlights: [] });
  assert.equal(hasEdits({ summary: "", highlights: null }), true);
});

test("nothing touched sends nothing", () => {
  assert.deepEqual(editedFields({ summary: null, highlights: null }), {});
  assert.equal(hasEdits({ summary: null, highlights: null }), false);
});
