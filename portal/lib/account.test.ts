// AA-761: the Account pages' pure helpers (lib/account.ts). Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  activityQuery,
  actorLabel,
  cleanFilters,
  connectionHealth,
  exportFilename,
  fmtDay,
  hasFilters,
  NO_FILTERS,
  usageMeter,
  type Quota,
} from "./account.ts";

const Q = (used: number, limit: number): Quota => ({
  plan_tier: "starter", tours_per_month: limit, rewrites_used: used,
  rewrites_remaining: Math.max(0, limit - used), resets_at: "2026-11-01",
});

test("meter: under, near and over the quota, with the reset date from /v1/quota", () => {
  const ok = usageMeter(Q(10, 50), { plan_attached: true, overage_usd: 0 });
  assert.equal(ok.state, "ok");
  assert.equal(ok.pct, 20);
  assert.equal(ok.remaining, 40);
  assert.equal(ok.resetsAt, "2026-11-01");
  assert.equal(usageMeter(Q(40, 50), { plan_attached: true }).state, "near");
  const over = usageMeter(Q(53, 50), { plan_attached: true, overage_usd: "6.00", overage_rate_usd_per_tour: "2" });
  assert.equal(over.state, "over");
  assert.equal(over.pct, 100);
  assert.equal(over.overTours, 3);
  assert.equal(over.overageUsd, 6);
  assert.equal(over.ratePerTour, 2);
  assert.equal(over.remaining, 0);
});

test("meter: no plan, unlimited, and nothing loaded yet", () => {
  assert.equal(usageMeter(Q(3, 50), { plan_attached: false }).state, "no_plan");
  const unl = usageMeter(Q(3, 999999), { plan_attached: true });
  assert.equal(unl.state, "unlimited");
  assert.equal(unl.quota, null);
  const none = usageMeter(null, null);
  assert.equal(none.used, 0);
  assert.equal(none.resetsAt, null);
  // a zero quota with usage is full, not a division by zero
  assert.equal(usageMeter(Q(2, 0), { plan_attached: true }).pct, 100);
});

test("activity query: empties dropped, dates checked, reversed dates swapped", () => {
  assert.equal(activityQuery(NO_FILTERS), "");
  assert.equal(activityQuery(NO_FILTERS, 2, 25), "?page=2&page_size=25");
  assert.equal(activityQuery({ category: "content", since: "2026-10-01", until: "2026-10-31" }, 1, 10),
    "?category=content&since=2026-10-01&until=2026-10-31&page=1&page_size=10");
  assert.equal(activityQuery({ category: "", since: "2026-10", until: "" }), "");
  assert.deepEqual(cleanFilters({ category: "", since: "2026-10-31", until: "2026-10-01" }),
    { category: "", since: "2026-10-01", until: "2026-10-31" });
  assert.equal(hasFilters({ category: "", since: "x", until: "" }), false);
  assert.equal(hasFilters({ category: "tours", since: "", until: "" }), true);
});

test("export file name follows the filters", () => {
  assert.equal(exportFilename(NO_FILTERS, "2026-10-25"), "activity-2026-10-25.csv");
  assert.equal(exportFilename({ category: "content", since: "2026-10-01", until: "" }, "2026-10-25"),
    "activity-content-2026-10-01-to-2026-10-25.csv");
});

test("labels and health", () => {
  assert.equal(actorLabel("you"), "You");
  assert.equal(actorLabel("adventure_asia"), "Adventure Asia");
  assert.equal(actorLabel("anything"), "Automatic");
  assert.equal(connectionHealth(null), "not_connected");
  assert.equal(connectionHealth({ connected: true, last_verified_at: "2026-10-01T00:00:00Z" }), "healthy");
  assert.equal(connectionHealth({ connected: true, last_verified_at: "x", last_verify_error: "401" }), "failing");
  assert.equal(connectionHealth({ connected: true }), "untested");
  assert.equal(fmtDay("2026-11-01"), "1 Nov 2026");
  assert.equal(fmtDay(null), "—");
});
