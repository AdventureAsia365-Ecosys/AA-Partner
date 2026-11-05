// AA-1036 (R3, S269 live gap) — the CRM's Settings > Add-ons page: a CRM-only owner asks for the Trip planner through
// the CRM API (relayed to CIS tenant.addons.v1), never through the studio.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  type AddOn,
  type AddOnRequest,
  CRM_ADD_ONS_PATH,
  decisionDate,
  decisionKey,
  loadErrorKey,
  orderAddOns,
  refusalKey,
} from "@/lib/addOns";
import { BFF_ROUTES, matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";

const request = (over: Partial<AddOnRequest> = {}): AddOnRequest => ({
  request_id: "5d2e3f4a-6b7c-4d8e-9fa0-1b2c3d4e5f60",
  product: "tripplanner",
  status: "pending",
  note: null,
  requested_at: "2026-11-21T10:00:00Z",
  decided_at: null,
  decision_note: null,
  setup: null,
  ...over,
});

const addOn = (product: AddOn["product"]): AddOn => ({
  product,
  state: "available",
  subscription_status: null,
  request: null,
  last_decision: null,
});

describe("Settings > Add-ons (CRM)", () => {
  it("the page lives in the CRM settings", () => {
    expect(CRM_ADD_ONS_PATH).toBe("/settings/add-ons");
    const page = path.resolve(__dirname, "..", "app", "(crm)", "settings", "add-ons", "page.tsx");
    const text = readFileSync(page, "utf8");
    expect(text).toContain('crmCall<AddOnList>(ctx.cfg, session, "GET", "add-ons")');
    expect(text).toContain('ctx.role === "owner"'); // non-owners are never sent to CIS
  });

  it("the BFF forwards only the two add-on routes, with the session's agency", () => {
    for (const [method, sub] of [["GET", "add-ons"], ["POST", "add-on-requests"]] as const) {
      const route = matchBff(method, sub, BFF_ROUTES);
      expect(route?.tenant).toBe(true);
      expect(route?.card).toBe("AA-1036");
    }
    expect(matchBff("POST", "add-ons", BFF_ROUTES)).toBeNull();
    expect(matchBff("GET", "add-on-requests", BFF_ROUTES)).toBeNull();
    expect(matchBff("DELETE", "add-on-requests", BFF_ROUTES)).toBeNull();
  });

  it("shows the Trip planner first", () => {
    expect(orderAddOns([addOn("crm"), addOn("tripplanner")]).map((a) => a.product)).toEqual(["tripplanner", "crm"]);
  });

  it("explains a list it cannot show", () => {
    expect(loadErrorKey(403, "role_forbidden")).toBe("addons.ownerOnly");
    expect(loadErrorKey(404, "addons_no_account")).toBe("addons.err.noAccount");
    expect(loadErrorKey(503, "addons_unavailable")).toBe("addons.err.load");
    expect(loadErrorKey(0, "api_unreachable")).toBe("addons.err.load");
  });

  it("names CIS's refusal reason, else a generic sentence", () => {
    expect(refusalKey(403, "role_forbidden")).toBe("addons.err.ownerOnly");
    expect(refusalKey(409, "tenant_not_in_service")).toBe("addons.err.notInService");
    expect(refusalKey(409, "already_subscribed")).toBe("addons.err.alreadyOn");
    expect(refusalKey(409, "addon_refused")).toBe("addons.err.send");
    expect(refusalKey(429, "too_many_requests")).toBe("addons.err.tooMany");
    expect(refusalKey(429, "tenant_busy")).toBe("addons.err.send"); // the CRM's own fair share, not CIS's daily cap
    expect(refusalKey(404, "addons_no_account")).toBe("addons.err.noAccount");
    expect(refusalKey(503, "addons_unavailable")).toBe("addons.err.send");
  });

  it("an approval says how its setup went; a decline names its request's date", () => {
    expect(decisionKey(request({ status: "declined" }))).toBe("addons.declined");
    expect(decisionKey(request({ status: "approved", setup: "in_progress" }))).toBe("addons.approvedSettingUp");
    expect(decisionKey(request({ status: "approved", setup: "failed" }))).toBe("addons.approvedSetupFailed");
    expect(decisionKey(request({ status: "approved", setup: "done" }))).toBe("addons.approved");
    const declined = request({ status: "declined", decided_at: "2026-11-22T10:00:00Z" });
    expect(decisionDate(declined)).toBe("2026-11-21T10:00:00Z");
    expect(decisionDate(request({ status: "approved", decided_at: "2026-11-22T10:00:00Z" }))).toBe("2026-11-22T10:00:00Z");
  });

  it("every add-on sentence is in English and Vietnamese, with the same placeholders", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("addons."));
    expect(keys.length).toBeGreaterThan(20);
    for (const k of keys) {
      const en = MESSAGES.en[k as keyof typeof MESSAGES.en];
      const vi = MESSAGES.vi[k as keyof typeof MESSAGES.vi];
      expect(vi, k).toBeTruthy();
      expect((vi.match(/\{\w+\}/g) ?? []).sort(), k).toEqual((en.match(/\{\w+\}/g) ?? []).sort());
    }
  });
});
