// AA-1139 (fix pass) — the Content studio's Trip planner page for an agency without the CRM add-on: the studio's
// route handler (app/api/studio/trip-planner) maps the CRM view's paths, checks the studio role first, reads CIS's
// refusal codes, and reaches the CRM API on server-only routes (lib/bff.ts) only.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { BFF_ROUTES, matchBff, SERVER_ONLY_ROUTES } from "@/lib/bff";
import {
  cisDetail,
  plannerAction,
  STUDIO_TRIP_PLANNER_PATH,
  studioMay,
  studioPlannerUrl,
} from "@/lib/studio/tripPlanner";

describe("studio trip planner actions", () => {
  it.each([
    ["GET", [], "page"],
    ["GET", ["leads"], "leads"],
    ["PUT", ["lead-route"], "lead-route"],
    ["PUT", ["appearance"], "appearance"],
    ["POST", ["webhook-key"], "webhook-key"],
    ["get", [], "page"],
  ] as const)("%s /%j is %s", (method, segments, action) => {
    expect(plannerAction(method, segments)).toBe(action);
  });

  it.each([
    ["POST", []],
    ["PUT", ["leads"]],
    ["GET", ["webhook-key"]],
    ["DELETE", ["lead-route"]],
    ["GET", ["leads", "extra"]],
    ["PUT", ["..", "appearance"]],
  ] as const)("%s /%j is not a route", (method, segments) => {
    expect(plannerAction(method, segments)).toBeNull();
  });

  it("lets the owner and editors read and only the owner change", () => {
    for (const action of ["page", "leads"] as const) {
      expect([studioMay("owner", action), studioMay("editor", action), studioMay("viewer", action), studioMay(null, action)])
        .toEqual([true, true, false, false]);
    }
    for (const action of ["lead-route", "appearance", "webhook-key"] as const) {
      expect([studioMay("owner", action), studioMay("editor", action), studioMay("viewer", action)]).toEqual([true, false, false]);
    }
  });

  it("maps the CRM view's paths to the studio handler", () => {
    expect(studioPlannerUrl("trip-planner/lead-route")).toBe("/api/studio/trip-planner/lead-route");
    expect(studioPlannerUrl("trip-planner/webhook-key")).toBe("/api/studio/trip-planner/webhook-key");
    expect(studioPlannerUrl("trip-planner")).toBe("/api/studio/trip-planner");
    expect(STUDIO_TRIP_PLANNER_PATH.startsWith("/portal/account/")).toBe(true);
  });
});

describe("CIS refusal codes", () => {
  it("prefers the header, then a structured or plain detail", () => {
    expect(cisDetail("lead_email_not_public", { detail: { detail: "other" } })).toBe("lead_email_not_public");
    expect(cisDetail(null, { detail: { detail: "tripplanner_not_active", message: "no planner" } })).toBe("tripplanner_not_active");
    expect(cisDetail(null, { detail: "studio_role_forbidden", message: "no" })).toBe("role_forbidden");
    expect(cisDetail(null, { detail: "Free text sentence." })).toBe("error");
    expect(cisDetail(null, null)).toBe("error");
  });
});

describe("the CRM API routes of the studio page", () => {
  it("are server-only, never on the browser BFF", () => {
    for (const [method, sub] of [["GET", "studio/trip-planner"], ["GET", "studio/trip-planner/leads"], ["POST", "studio/trip-planner/webhook-key"]] as const) {
      expect(matchBff(method, sub, SERVER_ONLY_ROUTES)?.tenant).toBe(true);
      expect(matchBff(method, sub, BFF_ROUTES)).toBeNull();
    }
    expect(matchBff("PUT", "studio/trip-planner/lead-route", SERVER_ONLY_ROUTES)).toBeNull();
  });

  it("the handler checks the role before any upstream and sends the studio tenant, not the request's", () => {
    const src = readFileSync(path.join(__dirname, "..", "app", "api", "studio", "trip-planner", "[[...path]]", "route.ts"), "utf8");
    const handler = src.slice(src.indexOf("async function handler("));
    expect(handler.indexOf("studioMay(")).toBeGreaterThan(handler.indexOf("requireStudio(req)"));
    expect(handler).toContain("studioMay(studioClaims(auth.token).csr, action) ? await run(req, auth, action)");
    expect(src).toContain("tid: auth.tenantId");
    expect(src).toContain("refuseForeignWrite(");
    expect(src).not.toMatch(/console\.(log|info)/);
  });
});
