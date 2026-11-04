// AA-1033 (S264 fix F1): the CRM terms of use must sit on a path the CRM web serves on the agency host. The ALB sends
// /terms, /privacy and /assets/* (and /portal*, /api/*, /tenant-login*, /crm*) elsewhere before the CRM web's default
// rule, so a CRM page or link there opens another service's page (the studio's partner terms for /terms).
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import {
  AGENCY_HOST_ROUTED_ELSEWHERE,
  CRM_TERMS_AI_HREF,
  CRM_TERMS_PATH,
  agencyHostRuleFor,
  albPathMatches,
} from "@/lib/legal";
import { gate } from "@/proxy";
import { testConfig } from "./helpers";

const WEB = join(__dirname, "..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const abs = join(dir, name);
    if (name === "node_modules" || name === ".next" || name === "core") return [];
    if (statSync(abs).isDirectory()) return sources(abs);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [abs] : [];
  });
}

describe("ALB path-pattern matching", () => {
  it("matches exact paths exactly and * across segments, case-sensitive", () => {
    expect(albPathMatches("/terms", "/terms")).toBe(true);
    expect(albPathMatches("/terms", "/terms/x")).toBe(false);
    expect(albPathMatches("/terms", "/legal/terms")).toBe(false);
    expect(albPathMatches("/terms", "/Terms")).toBe(false);
    expect(albPathMatches("/portal*", "/portal-assets/_next/static/a.js")).toBe(true);
    expect(albPathMatches("/crm/*", "/crm/v1/trips")).toBe(true);
    expect(albPathMatches("/crm/*", "/crmx")).toBe(false);
    expect(albPathMatches("/assets/*", "/assets/logo.svg")).toBe(true);
  });

  it("names the rule that takes a path away from the CRM web, ignoring the query and the fragment", () => {
    expect(agencyHostRuleFor("/terms#ai")).toBe("/terms");
    expect(agencyHostRuleFor("/privacy?x=1")).toBe("/privacy");
    expect(agencyHostRuleFor("/tenant-login")).toBe("/tenant-login*");
    expect(agencyHostRuleFor("/pipeline")).toBeNull();
  });

  it("keeps the studio's /terms, /privacy and /assets/* in the list (crm_agency_studio_x)", () => {
    for (const p of ["/terms", "/privacy", "/assets/*"]) expect(AGENCY_HOST_ROUTED_ELSEWHERE).toContain(p);
  });
});

describe("CRM terms of use path", () => {
  it("is served by the CRM web on the agency host (no studio or API rule matches it)", () => {
    expect(agencyHostRuleFor(CRM_TERMS_PATH)).toBeNull();
    expect(agencyHostRuleFor(CRM_TERMS_AI_HREF)).toBeNull();
    expect(CRM_TERMS_AI_HREF).toBe(`${CRM_TERMS_PATH}#ai`);
  });

  it("is the route of the CRM terms page", () => {
    expect(existsSync(join(WEB, "app", "(crm)", ...CRM_TERMS_PATH.split("/").filter(Boolean), "page.tsx"))).toBe(true);
    expect(existsSync(join(WEB, "app", "(crm)", "terms"))).toBe(false);
  });

  it("is what the shell footer and Settings > AI link to", () => {
    expect(readFileSync(join(WEB, "app/_kit/local/CrmShell.tsx"), "utf8")).toContain("href={CRM_TERMS_PATH}");
    expect(readFileSync(join(WEB, "components/settings/AiSettingsView.tsx"), "utf8")).toContain("href={CRM_TERMS_AI_HREF}");
  });

  it("no CRM web source links a studio-owned legal path (/terms, /privacy) on its own host", () => {
    const files = [...sources(join(WEB, "app")), ...sources(join(WEB, "components")), ...sources(join(WEB, "lib"))];
    expect(files.length).toBeGreaterThan(50); // the scan sees the code
    const hits = files
      // the rule list itself, and (AA-1036) the studio's map of its old legal paths to /legal/* (308 sources)
      .filter((abs) => !["lib/legal.ts", "lib/studio/legal.ts"].includes(relative(WEB, abs)))
      .map((abs) => ({ path: relative(WEB, abs), text: readFileSync(abs, "utf8") }))
      .filter((f) => /["'`]\/(terms|privacy)(?=["'`#?])/.test(f.text))
      .map((f) => f.path);
    expect(hits).toEqual([]);
  });
});

describe("CRM terms of use behind the session gate", () => {
  it("a signed-out visitor goes to /auth/refresh and back to the terms (the post-deploy crm-terms edge check)", () => {
    testConfig();
    const headers = new Headers({ host: "partner.example.test" });
    const res = gate(new NextRequest(`https://partner.example.test${CRM_TERMS_PATH}`, { headers }));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("https://partner.example.test/auth/refresh?next=%2Flegal%2Fterms");
  });
});
