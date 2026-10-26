// Route handlers end to end with a stubbed fetch: sign-in -> callback -> session; refresh; agency pick; BFF;
// sign-out (same-origin only); healthz.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as agencyGet, POST as agencyPost } from "@/app/auth/agency/route";
import { GET as bffGet, POST as bffPost } from "@/app/bff/[...path]/route";
import { GET as callback } from "@/app/auth/callback/route";
import { GET as refresh } from "@/app/auth/refresh/route";
import { GET as signIn } from "@/app/auth/sign-in/route";
import { POST as signOut } from "@/app/auth/sign-out/route";
import { GET as healthz } from "@/app/healthz/route";
import { GET as searchGet, POST as searchPost } from "@/app/(crm)/clients/search/route";
import { POST as setLocale } from "@/app/prefs/locale/route";
import { openSearch, searchCookieName } from "@/lib/clientSearch";
import { cookieNames, readSession, refreshCookie, sessionCookie } from "@/lib/session";
import { cookieHeader, ENV, fakeAccessToken, testConfig } from "./helpers";

const ORIGIN = "https://partner.example.test";
let cfg = testConfig();

beforeEach(() => {
  cfg = testConfig();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function request(path: string, init: { method?: string; cookie?: string; headers?: Record<string, string>; body?: BodyInit } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", "partner.example.test");
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(`${ORIGIN}${path}`, { method: init.method ?? "GET", headers, body: init.body });
}

function jarOf(cookie: string) {
  const map = new Map(cookie.split("; ").map((p) => [p.slice(0, p.indexOf("=")), { value: p.slice(p.indexOf("=") + 1) }]));
  return { get: (n: string) => map.get(n) };
}

function signedIn(tid: string | null = "tenant-a", si?: 1): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid, si });
  const r = refreshCookie(cfg, "rt-1", "sub-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

const MEMBERSHIPS = {
  memberships: [
    { tenant_id: "tenant-a", member_id: "m1", member_role: "owner", display_name: "Lữ hành Sen Vàng", default_locale: "vi-VN", tenant_status: "active" },
    { tenant_id: "tenant-l", member_id: "m2", member_role: "agent", display_name: "Locked Co", default_locale: "en-AU", tenant_status: "locked" },
  ],
};

describe("sign-in and callback", () => {
  it("round-trips: state checked, code exchanged, session holds the token, tx cookie expired", async () => {
    const start = signIn(request("/auth/sign-in?next=/clients"));
    expect(start.status).toBe(307);
    const authorize = new URL(start.headers.get("location")!);
    expect(authorize.origin).toBe("https://auth.example.test");
    const state = authorize.searchParams.get("state")!;
    const txCookie = cookieHeader(start);
    expect(txCookie).toContain(cookieNames(cfg).tx);

    const at = fakeAccessToken();
    const tokenFetch = vi.fn(async () => Response.json({ access_token: at, refresh_token: "rt-new", expires_in: 1800 }));
    vi.stubGlobal("fetch", tokenFetch);
    const done = await callback(request(`/auth/callback?code=abc&state=${state}`, { cookie: txCookie }));
    expect(done.status).toBe(307);
    expect(done.headers.get("location")).toBe(`${ORIGIN}/clients`);
    const session = readSession(cfg, jarOf(cookieHeader(done)));
    expect(session).toMatchObject({ at, sub: "sub-1", tid: null, si: 1 }); // AA-940: a sign-in to record on the pick
    expect(done.headers.getSetCookie().some((c) => c.startsWith(`${cookieNames(cfg).tx}=;`))).toBe(true);
  });

  it("refuses a wrong state, a missing attempt and a Cognito error", async () => {
    const start = signIn(request("/auth/sign-in"));
    const txCookie = cookieHeader(start);
    for (const path of ["/auth/callback?code=abc&state=wrong", "/auth/callback?error=access_denied&state=x"]) {
      const res = await callback(request(path, { cookie: txCookie }));
      expect(res.headers.get("location")).toBe(`${ORIGIN}/signed-out?reason=failed`);
    }
    const noTx = await callback(request("/auth/callback?code=abc&state=x"));
    expect(noTx.headers.get("location")).toBe(`${ORIGIN}/signed-out?reason=failed`);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 400 })));
    const state = new URL(start.headers.get("location")!).searchParams.get("state")!;
    const used = await callback(request(`/auth/callback?code=abc&state=${state}`, { cookie: txCookie }));
    expect(used.headers.get("location")).toBe(`${ORIGIN}/signed-out?reason=failed`);
  });

  it("goes to the not-configured page when identity values are missing", () => {
    cfg = testConfig({ COGNITO_CLIENT_SECRET: "" });
    expect(signIn(request("/auth/sign-in")).headers.get("location")).toBe(`${ORIGIN}/signed-out?reason=unavailable`);
  });
});

describe("refresh", () => {
  it("renews the token, keeps the agency and rotates the refresh token", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: fakeAccessToken(), refresh_token: "rt-2", expires_in: 1800 })));
    const res = await refresh(request("/auth/refresh?next=/tasks", { cookie: signedIn("tenant-a") }));
    expect(res.headers.get("location")).toBe(`${ORIGIN}/tasks`);
    const jar = jarOf(cookieHeader(res));
    expect(readSession(cfg, jar)?.tid).toBe("tenant-a");
  });

  it("falls back to sign-in without a refresh token or when Cognito refuses it", async () => {
    const none = await refresh(request("/auth/refresh?next=/tasks"));
    expect(none.headers.get("location")).toBe(`${ORIGIN}/auth/sign-in?next=%2Ftasks`);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 400 })));
    const refused = await refresh(request("/auth/refresh?next=/tasks", { cookie: signedIn() }));
    expect(refused.headers.get("location")).toContain("/auth/sign-in");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: fakeAccessToken({ sub: "someone-else" }), expires_in: 1800 })));
    const swapped = await refresh(request("/auth/refresh", { cookie: signedIn() }));
    expect(swapped.headers.get("location")).toContain("/auth/sign-in");
  });
});

describe("agency pick", () => {
  it("auto-picks the only active agency and refuses one the person is not in", async () => {
    const api = vi.fn(async (_url: string, _init?: RequestInit) => Response.json(MEMBERSHIPS));
    vi.stubGlobal("fetch", api);
    const signIns = () => api.mock.calls.filter(([url]) => String(url).endsWith("/crm/v1/audit/sign-in"));
    const auto = await agencyGet(request("/auth/agency?next=/clients", { cookie: signedIn(null, 1) }));
    expect(auto.headers.get("location")).toBe(`${ORIGIN}/clients`);
    const picked = readSession(cfg, jarOf(cookieHeader(auto)));
    expect(picked?.tid).toBe("tenant-a");
    expect(picked?.si).toBeUndefined(); // the one-shot flag is used up
    // AA-940: the pick right after a sign-in records it in the picked agency's audit (POST, its tenant header).
    expect(signIns().length).toBe(1);
    expect(signIns()[0][1]?.method).toBe("POST");
    expect(new Headers(signIns()[0][1]?.headers).get("X-AA-Tenant-Id")).toBe("tenant-a");
    // A later GET (a sibling *.lumiguides.it.com page can make the browser send one) has no flag: no event.
    const again = await agencyGet(request("/auth/agency?next=/clients", { cookie: signedIn("tenant-a") }));
    expect(again.headers.get("location")).toBe(`${ORIGIN}/clients`);
    expect(signIns().length).toBe(1);

    const form = (tenant: string) => {
      const f = new FormData();
      f.set("tenant_id", tenant);
      f.set("next", "/");
      return f;
    };
    const ok = await agencyPost(request("/auth/agency", { method: "POST", cookie: signedIn(null), headers: { origin: ORIGIN }, body: form("tenant-a") }));
    expect(ok.status).toBe(303);
    expect(readSession(cfg, jarOf(cookieHeader(ok)))?.tid).toBe("tenant-a");
    expect(signIns().length).toBe(2); // a chooser pick (same-site POST) always records
    for (const tenant of ["tenant-l", "tenant-x"]) {
      const no = await agencyPost(request("/auth/agency", { method: "POST", cookie: signedIn(null), headers: { origin: ORIGIN }, body: form(tenant) }));
      expect(no.headers.get("location")).toContain("/select-agency");
      expect(no.headers.getSetCookie().length).toBe(0);
    }
    const cross = await agencyPost(request("/auth/agency", { method: "POST", cookie: signedIn(null), headers: { origin: "https://evil.example.test" }, body: form("tenant-a") }));
    expect(cross.status).toBe(403);
  });
});

describe("BFF", () => {
  it("forwards an allowed call with the session's token and agency, never the browser's", async () => {
    const upstream = vi.fn(async () => Response.json({ tenant_id: "tenant-a", member_role: "owner" }));
    vi.stubGlobal("fetch", upstream);
    const res = await bffGet(
      request("/bff/me", { cookie: signedIn("tenant-a"), headers: { "x-aa-tenant-id": "tenant-evil", authorization: "Bearer stolen" } }),
      { params: Promise.resolve({ path: ["me"] }) },
    );
    expect(res.status).toBe(200);
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${ENV.CRM_API_ORIGIN}/crm/v1/me`);
    const h = init.headers as Headers;
    expect(h.get("x-aa-tenant-id")).toBe("tenant-a");
    expect(h.get("authorization")).not.toBe("Bearer stolen");
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("refuses unlisted paths, traversal, missing sessions, missing agency and cross-site writes", async () => {
    const upstream = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", upstream);
    const p = (path: string[]) => ({ params: Promise.resolve({ path }) });
    expect((await bffGet(request("/bff/x", { cookie: signedIn() }), p(["tenants"]))).status).toBe(404);
    expect((await bffGet(request("/bff/x", { cookie: signedIn() }), p(["..", "ops"]))).status).toBe(400);
    expect((await bffGet(request("/bff/me"), p(["me"]))).status).toBe(401);
    expect((await bffGet(request("/bff/me", { cookie: signedIn(null) }), p(["me"]))).status).toBe(409);
    expect((await bffPost(request("/bff/me", { method: "POST", cookie: signedIn(), headers: { origin: "https://evil.example.test" } }), p(["me"]))).status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("down"); }));
    expect((await bffGet(request("/bff/memberships", { cookie: signedIn() }), p(["memberships"]))).status).toBe(502);
  });
});

describe("sign-out, locale, healthz", () => {
  it("signs out same-origin only, revokes and clears every cookie", async () => {
    const revoke = vi.fn(async () => new Response("", { status: 200 }));
    vi.stubGlobal("fetch", revoke);
    const cross = await signOut(request("/auth/sign-out", { method: "POST", cookie: signedIn(), headers: { origin: "https://evil.example.test" } }));
    expect(cross.status).toBe(403);
    const res = await signOut(request("/auth/sign-out", { method: "POST", cookie: signedIn(), headers: { origin: ORIGIN } }));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toContain("https://auth.example.test/logout");
    expect(revoke).toHaveBeenCalledTimes(1);
    // AA-979 P2b: 3 CRM web cookies + the 5 cis_* Content studio cookies.
    expect(res.headers.getSetCookie().filter((c) => /Max-Age=0/i.test(c)).length).toBe(8);
  });

  it("stores a valid locale and refuses others", async () => {
    const f = new FormData();
    f.set("locale", "vi-VN");
    f.set("next", "/settings");
    const ok = await setLocale(request("/prefs/locale", { method: "POST", headers: { origin: ORIGIN }, body: f }));
    expect(ok.status).toBe(303);
    expect(ok.headers.getSetCookie()[0]).toContain("aa_crm_locale=vi-VN");
    const bad = new FormData();
    bad.set("locale", "xx");
    expect((await setLocale(request("/prefs/locale", { method: "POST", headers: { origin: ORIGIN }, body: bad }))).status).toBe(422);
  });

  it("client search keeps the term out of the URL (AA-937)", async () => {
    const f = new FormData();
    f.set("q", "  anna@example.test ");
    f.set("owner", "me");
    const ok = await searchPost(request("/clients/search", { method: "POST", cookie: signedIn(), headers: { origin: ORIGIN }, body: f }));
    expect(ok.status).toBe(303);
    const location = ok.headers.get("location")!;
    expect(location).toBe(`${ORIGIN}/clients?s=1&owner=me`);
    expect(location).not.toContain("anna");
    const set = ok.headers.getSetCookie().find((c) => c.startsWith(`${searchCookieName(cfg)}=`))!;
    expect(set).toMatch(/Path=\/clients/);
    expect(set).toMatch(/HttpOnly/i);
    expect(set).not.toContain("anna");
    const sealed = set.slice(set.indexOf("=") + 1, set.indexOf(";"));
    const session = readSession(cfg, jarOf(signedIn()));
    expect(openSearch(cfg, session, sealed)).toBe("anna@example.test");
    const empty = new FormData();
    empty.set("q", " ");
    const cleared = await searchPost(request("/clients/search", { method: "POST", cookie: signedIn(), headers: { origin: ORIGIN }, body: empty }));
    expect(cleared.headers.get("location")).toBe(`${ORIGIN}/clients`);
    expect(cleared.headers.getSetCookie().some((c) => /Max-Age=0/i.test(c))).toBe(true);
    const cross = await searchPost(request("/clients/search", { method: "POST", cookie: signedIn(), headers: { origin: "https://evil.example.test" }, body: f }));
    expect(cross.status).toBe(403);
    expect((await searchGet(request("/clients/search"))).headers.get("location")).toBe(`${ORIGIN}/clients`);
  });

  it("healthz answers without any dependency", async () => {
    const body = await healthz().json();
    expect(body).toMatchObject({ status: "ok", service: "aa-crm-web", git_sha: "abc123", auth_configured: true });
  });
});
