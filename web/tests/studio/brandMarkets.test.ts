// web/tests/studio/brandMarkets.test.ts — AA-1080: Brand Identity market note + brief review notes (vitest).
import assert from "node:assert/strict";
import { test } from "vitest";

import { countryName, draftedFields, marketNoteLines, wordList } from "@/lib/studio/brandMarkets";

test("a partly unsupported agency gets one line per unsupported market with the market used instead", () => {
  const lines = marketNoteLines({
    countries: ["AE", "SG", "UK", "AU"], supported: ["UK", "AU"], unsupported: ["AE", "SG"], unknown: [],
    seo_market: "UK", defaulted: false,
    notes: [{ country: "AE", reason: "no_search_data", uses: "UK" }, { country: "SG", reason: "no_search_data", uses: "UK" }],
  });
  assert.deepEqual(lines, {
    seoMarket: "United Kingdom", defaulted: false, unknown: [],
    unsupported: [{ country: "UAE", uses: "United Kingdom", reason: "no_search_data" },
                  { country: "Singapore", uses: "United Kingdom", reason: "no_search_data" }],
  });
});

test("an older API answer without notes still names the unsupported markets", () => {
  const lines = marketNoteLines({ countries: ["AE"], unsupported: ["AE"], unknown: ["Atlantis"] });
  assert.equal(lines?.seoMarket, "United States");
  assert.equal(lines?.defaulted, true);
  assert.deepEqual(lines?.unsupported, [{ country: "UAE", uses: "United States", reason: "no_search_data" }]);
  assert.deepEqual(lines?.unknown, ["Atlantis"]);
});

test("no markets, no note", () => {
  assert.equal(marketNoteLines(null), null);
  assert.equal(marketNoteLines({ countries: [], unsupported: [], unknown: [] }), null);
});

test("drafted fields come back in form order and unknown names are ignored", () => {
  assert.deepEqual(draftedFields(["signature_words", "system_prompt", "x", "default_cta"]),
                   ["default_cta", "system_prompt", "signature_words"]);
  assert.deepEqual(draftedFields(undefined), []);
});

test("word lists drop blanks and case-insensitive duplicates", () => {
  assert.deepEqual(wordList(" slow road, , Slow Road, maker visit "), ["slow road", "maker visit"]);
  assert.equal(countryName("ZZ"), "ZZ");
});
