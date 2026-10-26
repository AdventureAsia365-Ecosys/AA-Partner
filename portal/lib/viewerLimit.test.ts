// AA-979 P2b (critic S2): the per-browser limit of the API-key sign-in (lib/viewerLimit.ts). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { ViewerLimiter, viewerKey } from "./viewerLimit.ts";

test("each browser has its own window; no header means no limit here", () => {
  let now = 0;
  const limiter = new ViewerLimiter(3, 1000, () => now);
  for (let i = 0; i < 3; i++) assert.equal(limiter.allow("1.2.3.4"), true);
  assert.equal(limiter.allow("1.2.3.4"), false);
  assert.equal(limiter.allow("5.6.7.8"), true); // another browser behind the same NAT is not blocked
  assert.equal(limiter.allow(null), true);
  now = 1000;
  assert.equal(limiter.allow("1.2.3.4"), true); // a new window
});

test("CloudFront-Viewer-Address keys: the IP, IPv6 by its /64", () => {
  assert.equal(viewerKey("203.0.113.9:51234"), "203.0.113.9");
  assert.equal(viewerKey("2001:db8:1:2:3:4:5:6:443"), "2001:db8:1:2::/64");
  assert.equal(viewerKey("[2001:db8:1:2::9]:443"), "2001:db8:1:2::/64");
  assert.equal(viewerKey(null), null);
  assert.equal(viewerKey(""), null);
});
