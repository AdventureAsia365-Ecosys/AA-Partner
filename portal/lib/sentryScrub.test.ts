// AA-979 P2b: nothing personal or secret leaves for Sentry (lib/sentryScrub.ts). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { scrubEvent } from "./sentryScrub.ts";

test("drops the user, cookies, credentials, body and query string", () => {
  const event = scrubEvent({
    message: "boom",
    user: { email: "a@b.io" },
    request: {
      url: "https://portal.example.test/portal/content?piece=1&token=x",
      cookies: { cis_tenant_token: "t" },
      data: "{}",
      query_string: "token=x",
      headers: { Authorization: "Bearer t", Cookie: "c", "X-API-Key": "k", Accept: "text/html" },
    },
  });
  assert.deepEqual(event, {
    message: "boom",
    request: { url: "https://portal.example.test/portal/content", headers: { Accept: "text/html" } },
  });
  assert.equal(scrubEvent(null), null);
});
