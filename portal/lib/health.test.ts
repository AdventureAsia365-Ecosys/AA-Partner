// AA-993 V3: the studio's liveness body (lib/health.ts), read by the post-deploy verify (.github/verify/crm-portal.rows).
// Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { healthBody } from "./health.ts";

test("the body carries the commit and version baked into the image", () => {
  assert.deepEqual(healthBody({ GIT_SHA: "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e", APP_VERSION: "0.9.0" }), {
    ok: true,
    service: "aa-partner-portal",
    version: "0.9.0",
    sha: "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e",
  });
});

test("unset build values read unknown (the verify then goes red on the sha row)", () => {
  const body = healthBody({});
  assert.equal(body.sha, "unknown");
  assert.equal(body.version, "unknown");
});
