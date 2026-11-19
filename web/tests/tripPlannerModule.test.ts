// AA-1139 — the agency app's TripPlanner module (Settings > Add-ons > Trip planner): the CRM API relays to CIS (site
// settings) and TripPlanner (leads, usage, key); these are the page's pure helpers, the BFF routes and the strings.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { BFF_ROUTES, matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  domainTone,
  loadErrorKey,
  leadTone,
  type PlannerUsage,
  plannerLinks,
  refusalField,
  refusalKey,
  routeProblem,
  sessionsPercent,
  sessionsTone,
  themeToSend,
  TRIP_PLANNER_PATH,
  type TripPlannerModule,
} from "@/lib/tripPlannerModule";

const planner = (over: Partial<TripPlannerModule> = {}): TripPlannerModule => ({
  active: true,
  in_service: true,
  planner: { url: "https://planner.example.invalid/brindle-coast/plan", domains: ["plan.brindlecoast.example.com"], launcher: true },
  domains: [{ domain: "plan.brindlecoast.example.com", status: "active" }],
  crm_core: false,
  lead_route: { lead_channel: "email", lead_webhook_url: null, lead_email: "desk@brindlecoast.example.com", effective_channel: "email", updated_at: null },
  theme: { accent: "#2a6f97", "on-accent": "#ffffff" },
  locale: "en",
  ai_sessions_monthly: 300,
  ...over,
});

const usage = (used: number, quota: number | null): PlannerUsage => ({
  month: "2026-11",
  ai_allowance: { used, quota },
  leads_24h: { count: 1, cap: 200 },
});

describe("Settings > Add-ons > Trip planner", () => {
  it("the page lives under the CRM's add-ons and asks the assistant nothing", () => {
    expect(TRIP_PLANNER_PATH).toBe("/settings/add-ons/trip-planner");
    const page = path.resolve(__dirname, "..", "app", "(crm)", "settings", "add-ons", "trip-planner", "page.tsx");
    const text = readFileSync(page, "utf8");
    expect(text).toContain('crmCall<PlannerPage>(ctx.cfg, session, "GET", "trip-planner")');
    expect(text).toContain('ctx.role === "owner" || ctx.role === "agent"');
  });

  it("the BFF forwards the module's routes only, with the session's agency", () => {
    for (const [method, sub] of [
      ["GET", "trip-planner"],
      ["GET", "trip-planner/leads"],
      ["PUT", "trip-planner/lead-route"],
      ["PUT", "trip-planner/appearance"],
      ["POST", "trip-planner/webhook-key"],
    ] as const) {
      const route = matchBff(method, sub, BFF_ROUTES);
      expect(route?.tenant).toBe(true);
      expect(route?.card).toBe("AA-1139");
    }
    expect(matchBff("GET", "trip-planner/webhook-key", BFF_ROUTES)).toBeNull(); // the key is never read back
    expect(matchBff("DELETE", "trip-planner/lead-route", BFF_ROUTES)).toBeNull();
    expect(matchBff("PUT", "trip-planner/theme", BFF_ROUTES)).toBeNull();
  });

  it("lists the agency's own domains first, then the hosted planner", () => {
    expect(plannerLinks(planner())).toEqual([
      "https://plan.brindlecoast.example.com/plan",
      "https://planner.example.invalid/brindle-coast/plan",
    ]);
    expect(plannerLinks(planner({ planner: null }))).toEqual([]);
  });

  it("marks the month's AI sessions used up, and has no warning share of its own", () => {
    expect(sessionsPercent(usage(150, 300))).toBe(50);
    expect(sessionsPercent(usage(10, null))).toBeNull();
    expect(sessionsTone(usage(299, 300))).toBe("neutral");
    expect(sessionsTone(usage(300, 300))).toBe("danger");
    expect(sessionsTone(usage(9999, null))).toBe("neutral");
    expect(sessionsPercent(usage(400, 300))).toBe(100);
  });

  it("sends the edited colours in lower case and keeps the other tokens", () => {
    expect(themeToSend(planner().theme, { accent: "#AA3366", "accent-deep": "" })).toEqual({
      accent: "#aa3366",
      "on-accent": "#ffffff",
    });
    expect(themeToSend({}, { accent: "red" })).toEqual({});
  });

  it("checks the route before sending it, and maps CIS's reasons to a field and a sentence", () => {
    expect(routeProblem("webhook", "", "")).toBe("tp.err.lead_webhook_url_required");
    expect(routeProblem("webhook", "http://hooks.example.com", "")).toBe("tp.err.lead_webhook_url_not_https");
    expect(routeProblem("email", "", " ")).toBe("tp.err.lead_email_required");
    expect(routeProblem("none", "", "")).toBeNull();
    expect(refusalKey("lead_webhook_url_not_public")).toBe("tp.err.lead_webhook_url_not_public");
    expect(refusalKey("lead_email_not_public")).toBe("tp.err.lead_email_not_public");
    expect(refusalKey("theme_colour_invalid")).toBe("tp.err.theme");
    expect(refusalKey("role_forbidden")).toBe("tp.err.ownerOnly");
    expect(refusalKey("something_new")).toBe("tp.err.save");
    expect(refusalField("lead_webhook_url_invalid")).toBe("lead_webhook_url");
    expect(refusalField("lead_email_invalid")).toBe("lead_email");
    expect(refusalField("tripplanner_not_active")).toBeNull();
  });

  it("explains a page it cannot show", () => {
    expect(loadErrorKey(403, "role_forbidden")).toBe("tp.err.noAccess");
    expect(loadErrorKey(404, "tripplanner_no_account")).toBe("tp.err.noAccount");
    expect(loadErrorKey(503, "tripplanner_unavailable")).toBe("tp.err.cis");
    expect(loadErrorKey(0, "api_unreachable")).toBe("tp.err.load");
  });

  it("gives every domain and lead state a tone and a string in both languages", () => {
    for (const s of ["pending", "validating", "attaching", "active", "failed"] as const) {
      expect(domainTone(s)).toBeTruthy();
      expect(MESSAGES.en[`tp.domain.${s}`]).toBeTruthy();
      expect(MESSAGES.vi[`tp.domain.${s}`]).toBeTruthy();
    }
    for (const s of ["pending", "retrying", "delivered", "rejected", "expired"] as const) {
      expect(leadTone(s)).toBeTruthy();
      expect(MESSAGES.vi[`tp.lead.${s}`]).toBeTruthy();
    }
    for (const c of ["crm", "email", "webhook", "none"] as const) expect(MESSAGES.vi[`tp.channel.${c}`]).toBeTruthy();
  });

  it("never shows an issue id in the module's strings", () => {
    const text = JSON.stringify(Object.entries(MESSAGES.en).filter(([k]) => k.startsWith("tp.")));
    expect(text).not.toMatch(/AA-\d+/);
  });
});
