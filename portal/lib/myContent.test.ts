// AA-669: My Content's pure rules (lib/myContent.ts). Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  builtInViews,
  bulkPublishPlan,
  canRegenerate,
  contentStatus,
  exportFormat,
  hasFilters,
  matchesFilters,
  monthStart,
  NO_FILTERS,
  pieceTitle,
  placeTopic,
  publishBlock,
  sanitizeView,
  wordCount,
  type ContentRow,
} from "./myContent.ts";

const row = (over: Partial<ContentRow> = {}): ContentRow => ({
  piece_id: "p1",
  request_id: "r1",
  channel: "blog",
  ready_state: "ready",
  content_text: "# Where Two Worlds Meet\n\nCross the bamboo bridge at dawn.",
  goal: { key: "awareness", label: "Awareness" },
  angle: { name: "Where Two Worlds Meet" },
  atom: { text: "Cross the bamboo bridge", activity_type: "trekking" },
  tour: { name: "Sapa Trek", destination: "Vietnam" },
  route_hub_name: null,
  route_segment_count: null,
  created_at: "2026-10-10T08:00:00Z",
  published_at: null,
  published_url: null,
  served_status: "served",
  ...over,
});

const BOTH = { wordpress: true, facebook: true };

test("status: published wins, then ready / writing / not ready", () => {
  assert.equal(contentStatus(row({ published_at: "2026-10-12T00:00:00Z" })), "published");
  assert.equal(contentStatus(row()), "ready");
  assert.equal(contentStatus(row({ ready_state: "in_progress", content_text: null })), "writing");
  assert.equal(contentStatus(row({ ready_state: "not_ready", content_text: null })), "not_ready");
});

test("words skip markdown marks and links; no text is null", () => {
  assert.equal(wordCount("# Title here\n\n**Bold** words, see https://x.example/y now."), 6);
  assert.equal(wordCount(""), 0);
  assert.equal(wordCount(null), null);
  assert.equal(wordCount("Đi bộ qua cầu tre"), 5);
});

test("title is the first heading, else the angle, else Untitled", () => {
  assert.equal(pieceTitle(row()), "Where Two Worlds Meet");
  assert.equal(pieceTitle(row({ content_text: "Plain social post", angle: { name: "Golden hour" } })), "Golden hour");
  assert.equal(pieceTitle(row({ content_text: null, angle: null })), "Untitled");
  assert.ok(pieceTitle(row({ content_text: `## ${"x".repeat(200)}` })).length <= 118);
});

test("place / topic: tour, route hub, activity, or nothing", () => {
  assert.equal(placeTopic(row()), "Sapa Trek · Vietnam");
  assert.equal(placeTopic(row({ tour: { name: "Vietnam: Ha Long Cruise", destination: "Vietnam" } })), "Vietnam: Ha Long Cruise");
  assert.equal(placeTopic(row({ tour: null, route_hub_name: "Hanoi", route_segment_count: 3 })), "Hanoi route · 3 stops");
  assert.equal(placeTopic(row({ tour: null, atom: { text: "x", activity_type: "street_food" } })), "Street food");
  assert.equal(placeTopic(row({ tour: null, atom: null })), null);
});

test("filters: channel, status, goal, place, date range on created or published", () => {
  const r = row({ published_at: "2026-10-20T10:00:00Z" });
  assert.ok(matchesFilters(r, NO_FILTERS));
  assert.ok(!matchesFilters(r, { ...NO_FILTERS, channel: "facebook" }));
  assert.ok(matchesFilters(r, { ...NO_FILTERS, status: "published" }));
  assert.ok(!matchesFilters(r, { ...NO_FILTERS, status: "ready" }));
  assert.ok(!matchesFilters(r, { ...NO_FILTERS, goal: "Promotion" }));
  assert.ok(matchesFilters(r, { ...NO_FILTERS, place: "sapa" }));
  assert.ok(!matchesFilters(r, { ...NO_FILTERS, place: "bangkok" }));
  assert.ok(matchesFilters(r, { ...NO_FILTERS, from: "2026-10-01", to: "2026-10-31" }));
  assert.ok(!matchesFilters(r, { ...NO_FILTERS, from: "2026-10-11" })); // created on the 10th
  assert.ok(matchesFilters(r, { ...NO_FILTERS, dateField: "published", from: "2026-10-15" }));
  assert.ok(!matchesFilters(row(), { ...NO_FILTERS, dateField: "published", from: "2026-10-01" })); // never published
  assert.ok(!hasFilters(NO_FILTERS) && hasFilters({ ...NO_FILTERS, place: "x" }));
});

test("built-in views: ready to publish, published this month (from the 1st)", () => {
  const now = new Date(2026, 9, 25, 15, 0);
  assert.equal(monthStart(now), "2026-10-01");
  const views = builtInViews(now);
  assert.deepEqual(views.map((v) => v.id), ["all", "ready-to-publish", "published-this-month"]);
  const month = views[2].filters;
  assert.equal(month.status, "published");
  assert.equal(month.dateField, "published");
  assert.equal(month.from, "2026-10-01");
  assert.ok(matchesFilters(row({ published_at: new Date(2026, 9, 3).toISOString() }), month));
  assert.ok(!matchesFilters(row({ published_at: new Date(2026, 8, 30).toISOString() }), month));
  assert.ok(matchesFilters(row(), views[1].filters) && !matchesFilters(row({ published_at: "2026-10-02T00:00:00Z" }), views[1].filters));
});

test("a stored view is sanitized, never trusted", () => {
  const got = sanitizeView({ filters: { status: "deleted", from: "yesterday", place: 7, dateField: "x" }, sorting: [{ id: "words", desc: true }, "bad"] });
  assert.deepEqual(got.filters, NO_FILTERS);
  assert.deepEqual(got.sorting, [{ id: "words", desc: true }]);
  assert.deepEqual(sanitizeView(null).filters, NO_FILTERS);
});

test("publish: ready + supported channel + its connection, else a reason", () => {
  assert.equal(publishBlock(row(), BOTH), null);
  assert.equal(publishBlock(row(), { wordpress: false, facebook: true }), "Connect WordPress to publish");
  assert.equal(publishBlock(row({ channel: "facebook" }), { wordpress: true, facebook: false }), "Connect Facebook to publish");
  assert.equal(publishBlock(row({ channel: "linkedin" }), BOTH), "Export this channel and post it yourself");
  assert.equal(publishBlock(row({ published_at: "2026-10-12T00:00:00Z" }), BOTH), "Already published");
  assert.equal(publishBlock(row({ ready_state: "in_progress" }), BOTH), "Not ready yet");
  assert.equal(publishBlock(row({ served_status: "retired" }), BOTH), "Publishing closed for this tour");
});

test("bulk publish: blog pieces with WordPress connected only", () => {
  const rows = [
    row({ piece_id: "a" }),
    row({ piece_id: "b", channel: "facebook" }),
    row({ piece_id: "c", published_at: "2026-10-12T00:00:00Z" }),
    row({ piece_id: "d", ready_state: "not_ready", content_text: null }),
  ];
  const plan = bulkPublishPlan(rows, BOTH);
  assert.deepEqual(plan.eligible.map((r) => r.piece_id), ["a"]);
  assert.equal(plan.skipped, 3);
  assert.equal(bulkPublishPlan(rows, { wordpress: false, facebook: true }).eligible.length, 0);
});

test("export format and regenerate", () => {
  assert.equal(exportFormat(row()), "html");
  assert.equal(exportFormat(row({ channel: "instagram" })), "text");
  assert.equal(exportFormat(row({ content_text: null })), null);
  assert.ok(canRegenerate(row()));
  assert.ok(!canRegenerate(row({ ready_state: "in_progress" })));
  assert.ok(!canRegenerate(row({ served_status: "paused" })));
});
