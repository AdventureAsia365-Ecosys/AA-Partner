// AA-671: the Marketplace's pure helpers (lib/tours.ts). Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  activityLabel,
  cardTags,
  coverSrcSet,
  facetOptions,
  fmtDuration,
  INTENSITY_LABELS,
  intensityLabel,
  hasCatalogFilters,
  isRecentlyAdded,
  marketplaceView,
  NO_CATALOG_FILTERS,
  pageCount,
  parseStringList,
  poolQuery,
  rewriteQuotaNote,
  themeTags,
} from "./tours.ts";
import { usageMeter, type Quota } from "./account.ts";

const Q = (used: number, limit: number): Quota => ({
  plan_tier: "starter", tours_per_month: limit, rewrites_used: used,
  rewrites_remaining: Math.max(0, limit - used), resets_at: "2026-11-01",
});

test("duration: one casing, nights dropped, plain text lower-cased", () => {
  assert.equal(fmtDuration("14 DAYS"), "14 days");
  assert.equal(fmtDuration("15 DAYS 14 NIGHTS"), "15 days");
  assert.equal(fmtDuration("1 day"), "1 day");
  assert.equal(fmtDuration("Half Day"), "half day");
  assert.equal(fmtDuration(null), "");
});

test("lists: JSON, JSON in JSON, objects, junk", () => {
  assert.deepEqual(parseStringList('["a", " b "]'), ["a", "b"]);
  assert.deepEqual(parseStringList(JSON.stringify(JSON.stringify(["x", "y"]))), ["x", "y"]);
  assert.deepEqual(parseStringList([{ keyword: "trek" }, { text: "rice" }, 3, null]), ["trek", "rice"]);
  assert.deepEqual(parseStringList("not json"), []);
  assert.deepEqual(parseStringList(null), []);
});

test("theme tags: distinct, case-insensitive, capped", () => {
  assert.deepEqual(themeTags('["Hanoi", "hanoi", "Ha Long", "Sapa", "Hue"]'), ["Hanoi", "Ha Long", "Sapa"]);
  assert.deepEqual(themeTags('["a","b"]', 1), ["a"]);
  assert.deepEqual(themeTags('["Vietnam", "food", "culture", "bikes"]', 3, ["vietnam", null]), ["food", "culture", "bikes"]);
});

test("recently added: inside the window only, never in the future", () => {
  const now = new Date("2026-10-28T12:00:00Z");
  assert.equal(isRecentlyAdded("2026-10-20T00:00:00Z", now), true);
  assert.equal(isRecentlyAdded("2026-10-01T00:00:00Z", now), false);
  assert.equal(isRecentlyAdded("2026-11-01T00:00:00Z", now), false);
  assert.equal(isRecentlyAdded("garbage", now), false);
  assert.equal(isRecentlyAdded(null, now), false);
});

test("pool query: paging, trimmed search, country, duration ranges", () => {
  assert.equal(poolQuery(NO_CATALOG_FILTERS, 1), "page=1&page_size=24");
  assert.equal(poolQuery({ ...NO_CATALOG_FILTERS, search: "  bike ", country: "Vietnam", duration: "4-7" }, 2),
    "page=2&page_size=24&search=bike&country=Vietnam&duration_min=4&duration_max=7");
  assert.equal(poolQuery({ ...NO_CATALOG_FILTERS, duration: "8+" }, 0), "page=1&page_size=24&duration_min=8");
  assert.equal(hasCatalogFilters(NO_CATALOG_FILTERS), false);
  assert.equal(hasCatalogFilters({ ...NO_CATALOG_FILTERS, search: " " }), false);
  assert.equal(hasCatalogFilters({ ...NO_CATALOG_FILTERS, country: "Laos" }), true);
  assert.equal(pageCount(0), 1);
  assert.equal(pageCount(25), 2);
});

test("pool query: activity, intensity, added in the last N days", () => {
  assert.equal(poolQuery({ ...NO_CATALOG_FILTERS, activity: "trekking", intensity: "challenging", added: "30" }, 1),
    "page=1&page_size=24&activity=trekking&intensity=challenging&added_within_days=30");
  assert.equal(hasCatalogFilters({ ...NO_CATALOG_FILTERS, activity: "culinary" }), true);
  assert.equal(hasCatalogFilters({ ...NO_CATALOG_FILTERS, intensity: "easy" }), true);
  assert.equal(hasCatalogFilters({ ...NO_CATALOG_FILTERS, added: "14" }), true);
});

test("facet labels and options: known codes in English, unknown ones readable, API order kept", () => {
  assert.equal(activityLabel("wildlife_nature"), "Wildlife & nature");
  assert.equal(activityLabel("bird_watching"), "Bird watching");
  assert.equal(intensityLabel("challenging"), "Challenging");
  assert.deepEqual(facetOptions(undefined, INTENSITY_LABELS, intensityLabel).map((o) => o.value),
    ["easy", "moderate", "challenging", "strenuous"]);
  assert.deepEqual(facetOptions(["culinary", "new_code"], {}, activityLabel),
    [{ value: "culinary", label: "Food & cooking" }, { value: "new_code", label: "New code" }]);
});

test("card tags: activities first, theme tags when the tour has none, at most 3", () => {
  assert.deepEqual(cardTags(["trekking", "culinary"], ["Hanoi"]), ["Trekking & hiking", "Food & cooking"]);
  assert.deepEqual(cardTags([], ["Hanoi", "Food"]), ["Hanoi", "Food"]);
  assert.deepEqual(cardTags(undefined, ["a", "b", "c", "d"]), ["a", "b", "c"]);
});

test("cover srcset: both renditions with their widths, nothing when one is missing or both are the same", () => {
  assert.equal(coverSrcSet("https://x/p/1?size=small", "https://x/p/1?size=large"),
    "https://x/p/1?size=small 600w, https://x/p/1?size=large 1600w");
  assert.equal(coverSrcSet("https://x/p/1?size=small", null), undefined);
  assert.equal(coverSrcSet(undefined, "https://x/p/1?size=large"), undefined);
  assert.equal(coverSrcSet(" ", "https://x/p/1?size=large"), undefined);
  assert.equal(coverSrcSet("https://x/a", "https://x/a"), undefined);
});

test("view: only 'mine' selects the tenant's catalog", () => {
  assert.equal(marketplaceView("mine"), "mine");
  assert.equal(marketplaceView("all"), "all");
  assert.equal(marketplaceView(null), "all");
  assert.equal(marketplaceView("MINE"), "all");
});

test("quota note: left, near, crossing the quota, over, no plan, unlimited", () => {
  const billing = { plan_attached: true, overage_rate_usd_per_tour: 3 };
  assert.equal(rewriteQuotaNote(null), null);
  assert.deepEqual(rewriteQuotaNote(usageMeter(Q(2, 10), billing)), { tone: "neutral", text: "8 rewrites left this period." });
  assert.deepEqual(rewriteQuotaNote(usageMeter(Q(9, 10), billing)), { tone: "warning", text: "1 rewrite left this period." });
  assert.deepEqual(rewriteQuotaNote(usageMeter(Q(8, 10), billing), 5),
    { tone: "warning", text: "2 rewrites left this period: 3 of these 5 are billed as overage at $3 each." });
  assert.deepEqual(rewriteQuotaNote(usageMeter(Q(10, 10), billing)),
    { tone: "danger", text: "Your plan's rewrites are used up: this rewrite is billed as overage at $3 each." });
  assert.deepEqual(rewriteQuotaNote(usageMeter(Q(12, 10), { plan_attached: true }), 2),
    { tone: "danger", text: "Your plan's rewrites are used up: these 2 rewrites are billed as overage." });
  assert.equal(rewriteQuotaNote(usageMeter(Q(1, 10), { plan_attached: false }))?.text, "No plan attached yet: rewrites are not billed.");
  assert.equal(rewriteQuotaNote(usageMeter(Q(1, 999_999), billing))?.text, "Your plan has no rewrite limit.");
});
