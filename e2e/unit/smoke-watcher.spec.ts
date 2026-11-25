// e2e/unit/smoke-watcher.spec.ts — S294 (AA-1011 follow-up): the portal smoke's failure watcher records the CRM's
// /bff/ calls as well as the studio's /api/ ones (a 409 from /bff on the studio Integrations page showed only as a
// bare console error). No browser, no network.
import { expect, test } from "@playwright/test";
import { isWatchedPath } from "../smoke-portal/smoke-helpers";

test("the watcher records both BFFs and nothing else", () => {
  expect(isWatchedPath("/api/tenant/v1/integrations/wordpress")).toBe(true);
  expect(isWatchedPath("/api/studio/calendar")).toBe(true);
  expect(isWatchedPath("/bff/calendar/connections")).toBe(true);
  expect(isWatchedPath("/portal/account/integrations")).toBe(false);
  expect(isWatchedPath("/_next/static/chunk.js")).toBe(false);
  expect(isWatchedPath("/bffx")).toBe(false);
});
