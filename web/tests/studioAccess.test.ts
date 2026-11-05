// AA-1025 (S264): the "Open Content studio" link shows only for an agency that is one of the person's studios
// (lib/studioAccess.ts), and the studio's cookie list is the one sign-out clears (lib/studioCookies.ts).
import { readFileSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { STUDIO_COOKIES } from "@/lib/studioCookies";
import {
  ANSWER_TTL_MS, UNKNOWN_TTL_MS, decide, resetStudioAccessMemo, studioAccess, studioLink,
} from "@/lib/studioAccess";

const CIS = "https://cis.example.test";
const AGENCY = "0b0e6c9a-3d1f-4b7e-9a51-6f2d8c4e1a70";
const OTHER = "7c2d1e4f-5a6b-4c8d-9e0f-1a2b3c4d5e6f";
const SESSION = { at: "access-token", iss: "https://idp.example.test/pool", sub: "person-1", tid: AGENCY };

function answer(status: number, body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

beforeEach(() => resetStudioAccessMemo());

describe("decide", () => {
  it("yes only when the agency is one of the memberships", () => {
    expect(decide([{ tenant_id: OTHER }, { tenant_id: AGENCY }], AGENCY)).toBe("yes");
    expect(decide([{ tenant_id: OTHER }], AGENCY)).toBe("no");
    expect(decide([], AGENCY)).toBe("no");
    expect(decide(null, AGENCY)).toBe("unknown");
    expect(decide([{ tenant_id: AGENCY }], null)).toBe("unknown");
  });
});

describe("studioAccess", () => {
  it("asks CIS with the person's partner access token and hides the link for an agency without a studio", async () => {
    const f = answer(200, { memberships: [{ tenant_id: OTHER, tenant_name: "Lantern Bay Trips" }] });
    expect(await studioAccess(CIS, SESSION, f)).toBe("no");
    expect(f).toHaveBeenCalledTimes(1);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${CIS}/auth/tenant-memberships`);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer access-token");
    expect(studioLink("/portal/dashboard", "no")).toBeNull();
  });

  it("shows it when the agency is a studio of the person", async () => {
    const f = answer(200, { memberships: [{ tenant_id: AGENCY, tenant_name: "Lantern Bay Trips" }] });
    expect(await studioAccess(CIS, SESSION, f)).toBe("yes");
    expect(studioLink("/portal/dashboard", "yes")).toBe("/portal/dashboard");
  });

  it("a person with no studio at all (200, empty list) gets no link", async () => {
    expect(await studioAccess(CIS, SESSION, answer(200, { memberships: [] }))).toBe("no");
  });

  it("keeps the link when CIS cannot answer (throttled, down, refused, no CIS URL, no agency)", async () => {
    for (const [status, body] of [[429, { detail: "throttled" }], [503, { detail: "unavailable" }], [403, { detail: "forbidden" }], [200, {}]] as const) {
      resetStudioAccessMemo();
      expect(await studioAccess(CIS, SESSION, answer(status, body))).toBe("unknown");
    }
    resetStudioAccessMemo();
    const boom = vi.fn(async () => { throw new Error("network"); });
    expect(await studioAccess(CIS, SESSION, boom as unknown as typeof fetch)).toBe("unknown");
    const never = vi.fn();
    expect(await studioAccess("", SESSION, never as unknown as typeof fetch)).toBe("unknown");
    expect(await studioAccess(CIS, { ...SESSION, tid: null }, never as unknown as typeof fetch)).toBe("unknown");
    expect(never).not.toHaveBeenCalled();
    expect(studioLink("/portal/dashboard", "unknown")).toBe("/portal/dashboard");
  });

  it("memoises per person and agency (CIS throttles this route with the studio sign-in)", async () => {
    const f = answer(200, { memberships: [{ tenant_id: AGENCY }] });
    const now = 1_000_000;
    expect(await studioAccess(CIS, SESSION, f, now)).toBe("yes");
    expect(await studioAccess(CIS, SESSION, f, now + ANSWER_TTL_MS - 1)).toBe("yes");
    expect(f).toHaveBeenCalledTimes(1);
    // another agency of the same person, or another person, is asked again
    expect(await studioAccess(CIS, { ...SESSION, tid: OTHER }, f, now)).toBe("no");
    expect(await studioAccess(CIS, { ...SESSION, sub: "person-2" }, f, now)).toBe("yes");
    expect(f).toHaveBeenCalledTimes(3);
    expect(await studioAccess(CIS, SESSION, f, now + ANSWER_TTL_MS + 1)).toBe("yes");
    expect(f).toHaveBeenCalledTimes(4);
  });

  it("asks again soon after an unknown answer", async () => {
    const down = answer(503, { detail: "unavailable" });
    const now = 2_000_000;
    expect(await studioAccess(CIS, SESSION, down, now)).toBe("unknown");
    const up = answer(200, { memberships: [] });
    expect(await studioAccess(CIS, SESSION, up, now + UNKNOWN_TTL_MS - 1)).toBe("unknown");
    expect(await studioAccess(CIS, SESSION, up, now + UNKNOWN_TTL_MS + 1)).toBe("no");
  });
});

describe("wiring", () => {
  const root = path.resolve(__dirname, "..", "..");
  it("the CRM layout renders the studio link through studioAccess", () => {
    const layout = readFileSync(path.join(root, "web/app/(crm)/layout.tsx"), "utf8");
    expect(layout).toContain("studioAccess(ctx.cfg.cisApiUrl, session)");
    expect(layout).toContain("const contentHref = studioLink(studioHref, studio);");
    expect(layout).toContain("contentStudioHref={contentHref}");
    expect(layout).toContain("plannerHref={plannerHref(ctx.role)}"); // AA-1036 (R3, S269): the CRM's own Add-ons page
  });

  // AA-1036 (R3): the separate studio app is gone, so the studio's session helpers re-export the one cookie list
  // (lib/studioCookies.ts) instead of keeping a copy; sign-out and the studio can no longer drift apart.
  it("the studio session uses the one studio cookie list that sign-out clears", async () => {
    const studio = readFileSync(path.join(root, "web/lib/studio/studioSession.ts"), "utf8");
    expect(studio).toContain('export { STUDIO_COOKIES } from "../studioCookies";');
    expect(studio).not.toMatch(/export const STUDIO_COOKIES\b/);
    const reexported = await import("@/lib/studio/studioSession");
    expect(reexported.STUDIO_COOKIES).toBe(STUDIO_COOKIES);
    expect(STUDIO_COOKIES).toContain("cis_partner_ref");
    expect(STUDIO_COOKIES).toContain(reexported.PARTNER_REF_COOKIE);
  });
});
