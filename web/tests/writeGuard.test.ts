// AA-935 S249 sign-in check: sign-out answered 403 on Dev. Behind CloudFront -> ALB the Next standalone server sees
// its own bind address as the request URL (http://0.0.0.0:3000/...), so comparing the browser's Origin with
// request.nextUrl.origin refused every same-origin POST. These tests build requests exactly like that and expect the
// write routes to compare with WEB_PUBLIC_ORIGIN instead, while a foreign origin is still refused.
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST as agencyPost } from "@/app/auth/agency/route";
import { POST as bffPost } from "@/app/bff/[...path]/route";
import { POST as signOut } from "@/app/auth/sign-out/route";
import { POST as setLocale } from "@/app/prefs/locale/route";
import { refreshCookie, sessionCookie } from "@/lib/session";
import { expectedOrigin, refuseForeignWrite } from "@/lib/writeGuard";
import { fakeAccessToken, testConfig } from "./helpers";

const PUBLIC = "https://partner.example.test";
/** What Next standalone puts in request.url behind the ALB (HOSTNAME=0.0.0.0, PORT=3000). */
const BIND = "http://0.0.0.0:3000";
let cfg = testConfig();

beforeEach(() => {
  cfg = testConfig();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function behindAlb(p: string, init: { headers?: Record<string, string>; cookie?: string; body?: BodyInit } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", "partner.example.test");
  headers.set("x-forwarded-proto", "http");
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(`${BIND}${p}`, { method: "POST", headers, body: init.body });
}

function signedIn(tid: string | null = "tenant-a"): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid });
  const r = refreshCookie(cfg, "rt-1", "sub-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

/** What Chrome sends on the sidebar's sign-out form submit. */
const BROWSER_SAME_ORIGIN = { origin: PUBLIC, "sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate" };

describe("write guard compares with the public origin", () => {
  it("uses WEB_PUBLIC_ORIGIN, the request's own origin only when it is not configured", () => {
    const req = behindAlb("/auth/sign-out");
    expect(req.nextUrl.origin).toBe(BIND);
    expect(expectedOrigin(cfg, req)).toBe(PUBLIC);
    expect(expectedOrigin({ publicOrigin: "" }, req)).toBe(BIND);
  });

  it("lets a same-origin browser POST through and refuses other origins and cross-site fetches", () => {
    expect(refuseForeignWrite(cfg, behindAlb("/x", { headers: BROWSER_SAME_ORIGIN }))).toBeNull();
    expect(refuseForeignWrite(cfg, behindAlb("/x", { headers: { origin: "https://evil.example.test" } }))?.status).toBe(403);
    // Another *.lumiguides.it.com-like host is the same SITE but not the same origin (AA-911).
    expect(refuseForeignWrite(cfg, behindAlb("/x", { headers: { origin: "https://go.example.test" } }))?.status).toBe(403);
    // The server's bind address is not an origin a browser of this site can have.
    expect(refuseForeignWrite(cfg, behindAlb("/x", { headers: { origin: BIND } }))?.status).toBe(403);
    expect(refuseForeignWrite(cfg, behindAlb("/x", { headers: { "sec-fetch-site": "cross-site" } }))?.status).toBe(403);
    expect(refuseForeignWrite(cfg, behindAlb("/x", { headers: { "sec-fetch-site": "same-site" } }))?.status).toBe(403);
  });
});

describe("sign-out behind the ALB", () => {
  it("clears every session cookie, revokes the refresh token and goes to the managed-login logout", async () => {
    const revoke = vi.fn(async () => new Response("", { status: 200 }));
    vi.stubGlobal("fetch", revoke);
    const res = await signOut(behindAlb("/auth/sign-out", { headers: BROWSER_SAME_ORIGIN, cookie: signedIn() }));
    expect(res.status).toBe(303);
    const logout = new URL(res.headers.get("location")!);
    expect(`${logout.origin}${logout.pathname}`).toBe("https://auth.example.test/logout");
    expect(logout.searchParams.get("client_id")).toBe("testclient123");
    expect(logout.searchParams.get("logout_uri")).toBe(`${PUBLIC}/signed-out`);
    expect(revoke).toHaveBeenCalledTimes(1);
    const cleared = res.headers.getSetCookie().filter((c) => /Max-Age=0/i.test(c));
    expect(cleared.map((c) => c.split("=")[0]).sort()).toEqual(["__Host-aa_crm_s", "__Secure-aa_crm_r", "__Secure-aa_crm_tx"]);
    expect(cleared.find((c) => c.startsWith("__Host-aa_crm_s="))).toMatch(/Path=\/(;|$)/);
    expect(cleared.find((c) => c.startsWith("__Secure-aa_crm_r="))).toMatch(/Path=\/auth/);
  });

  it("still refuses a sign-out posted by another origin (logout CSRF) and leaves the cookies alone", async () => {
    const revoke = vi.fn();
    vi.stubGlobal("fetch", revoke);
    const res = await signOut(behindAlb("/auth/sign-out", { headers: { origin: "https://evil.example.test" }, cookie: signedIn() }));
    expect(res.status).toBe(403);
    expect(res.headers.getSetCookie()).toEqual([]);
    expect(revoke).not.toHaveBeenCalled();
  });
});

describe("the other write routes behind the ALB", () => {
  it("saves the language", async () => {
    const f = new FormData();
    f.set("locale", "vi-VN");
    f.set("next", "/settings");
    const res = await setLocale(behindAlb("/prefs/locale", { headers: BROWSER_SAME_ORIGIN, body: f }));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${PUBLIC}/settings`);
  });

  it("picks an agency and passes BFF writes on to the allow-list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ memberships: [
      { tenant_id: "tenant-a", member_id: "m1", member_role: "owner", display_name: "A", default_locale: "vi-VN", tenant_status: "active" },
    ] })));
    const f = new FormData();
    f.set("tenant_id", "tenant-a");
    f.set("next", "/");
    const pick = await agencyPost(behindAlb("/auth/agency", { headers: BROWSER_SAME_ORIGIN, cookie: signedIn(null), body: f }));
    expect(pick.status).toBe(303);
    expect(pick.headers.get("location")).toBe(`${PUBLIC}/`);
    const p = { params: Promise.resolve({ path: ["me"] }) };
    // No POST is on the allow-list yet: past the guard it is the allow-list's 404, not the guard's 403.
    expect((await bffPost(behindAlb("/bff/me", { headers: BROWSER_SAME_ORIGIN, cookie: signedIn() }), p)).status).toBe(404);
  });
});

describe("every write route uses the public-origin guard", () => {
  const appDir = path.resolve(__dirname, "..", "app");
  const routes: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (name === "route.ts") routes.push(full);
    }
  };
  walk(appDir);

  it("finds the route handlers", () => {
    expect(routes.length).toBeGreaterThanOrEqual(8);
  });

  it.each(routes.map((r) => [path.relative(appDir, r), r]))("%s", (_rel, file) => {
    const src = readFileSync(file, "utf8");
    // The vendored guard alone compares with the server's bind address; routes must not call it directly.
    expect(src).not.toMatch(/refuseCrossSiteWrite|isCrossSiteWrite|isSameOrigin/);
    if (/export (async )?function (POST|PUT|PATCH|DELETE)\b|export const (POST|PUT|PATCH|DELETE)\b/.test(src)) {
      expect(src).toMatch(/refuseForeignWrite\(/);
    }
  });
});
