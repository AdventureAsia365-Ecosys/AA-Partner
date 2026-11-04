// AA-1036 (R3 slice 1, root ADR 0019 d6 / d7): the Content studio inside the one agency app. The merged gate (host ->
// /staff -> /c -> /portal -> CRM), the studio's exchange step with CIS (fake fetch), the retired API-key sign-in,
// "/" landing on Content (WEB_CONTENT_HOME), the studio BFF's session rules, the app switcher and the legal moves.
// Fake names and hosts only.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import nextConfig from "@/next.config";
import { GET as appsGet } from "@/app/api/studio/apps/route";
import { POST as logoutPost } from "@/app/api/auth/tenant-logout/route";
import { POST as selectPost } from "@/app/api/auth/tenant-select/route";
import { GET as tenantMeGet } from "@/app/api/tenant/me/route";
import { GET as tenantProxyGet } from "@/app/api/tenant/[...path]/route";
import {
  ADD_ONS_PATH, addOnStatesFrom, cisAddOns, crmAccess, crmAccessFrom, crmApps, plannerHref, studioApps, UNKNOWN_ADD_ONS,
} from "@/lib/apps";
import { loadConfig } from "@/lib/config";
import { isApiKeySignIn, isPublicPartnerPath, isStudioAccessPage, isStudioBff, isStudioPage, STUDIO_HOME_PATH } from "@/lib/hosts";
import { activeSection, CRM_HOME_PATH } from "@/lib/nav";
import { sessionCookie } from "@/lib/session";
import { STUDIO_COOKIES } from "@/lib/studioCookies";
import { requireStudio, tenantVerifyCache } from "@/lib/studio/auth-server";
import { LEGACY_LEGAL_REDIRECTS, PRIVACY_PATH, TERMS_PATH } from "@/lib/studio/legal";
import { partnerRef } from "@/lib/studio/studioSession";
import { gate, proxy, studioGate } from "@/proxy";
import { cookieHeader, ENV, fakeAccessToken, testConfig } from "../helpers";

const ORIGIN = "https://partner.example.test";
const CIS = "https://cis.example.test";
const TENANT = "5b0c7a1e-2d3f-4e6a-9b8c-1d2e3f4a5b6c";
const OTHER = "6c1d8b2f-3e4a-4f7b-8c9d-2e3f4a5b6c7d";
const STUDIO_ENV = { CIS_API_URL: CIS };

let cfg = testConfig(STUDIO_ENV);
beforeEach(() => {
  cfg = testConfig(STUDIO_ENV);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function studioToken(claims: Record<string, unknown> = {}): string {
  const b = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b({ alg: "HS256" })}.${b({ sub: "t", amr: "partner", exp: Math.floor(Date.now() / 1000) + 900, ...claims })}.sig`;
}

function session(opts: { atExpIn?: number; sub?: string; tid?: string | null } = {}): string {
  const now = Math.floor(Date.now() / 1000);
  const c = sessionCookie(cfg, {
    at: fakeAccessToken({ sub: opts.sub ?? "sub-1" }),
    atExp: now + (opts.atExpIn ?? 1200),
    iss: "i",
    sub: opts.sub ?? "sub-1",
    cid: "c",
    tid: opts.tid === undefined ? TENANT : opts.tid,
  });
  return `${c.name}=${c.value}`;
}

function req(path: string, init: { cookie?: string; method?: string; host?: string; origin?: string; body?: BodyInit } = {}) {
  const headers = new Headers({ host: init.host ?? "partner.example.test" });
  if (init.cookie) headers.set("cookie", init.cookie);
  if (init.origin) headers.set("origin", init.origin);
  return new NextRequest(`${ORIGIN}${path}`, { method: init.method ?? "GET", headers, body: init.body });
}

/** A fake CIS: tenant-session answers `exchange`, verify-tenant answers `verify`. */
function fakeCis(exchange: { status: number; body: unknown }, verify: { status: number; body?: unknown } = { status: 200 }) {
  return vi.fn(async (url: string) => {
    if (url.endsWith("/auth/tenant-session")) return Response.json(exchange.body, { status: exchange.status });
    if (url.endsWith("/auth/verify-tenant")) {
      return Response.json(verify.body ?? { tenant_id: TENANT, name: "Harbour Lantern Tours", plan_tier: "growth" }, { status: verify.status });
    }
    return Response.json({ ok: true, url });
  });
}

const EXCHANGED = (token = studioToken()) => ({
  status: 200,
  body: { token, tenant_id: TENANT, tenant_name: "Harbour Lantern Tours", plan_tier: "growth", content_studio_role: "owner", expires_in: 900 },
});

describe("merged gate order (host -> /staff -> /c -> /portal -> CRM)", () => {
  it("an unknown Host is 404 before any studio rule", () => {
    expect(gate(req("/portal/dashboard", { host: "evil.example.test" })).status).toBe(404);
    expect(gate(req("/api/tenant/me", { host: "evil.example.test" })).status).toBe(404);
  });

  it("the client host serves no studio path", () => {
    for (const p of ["/portal/dashboard", "/api/tenant/me", "/portal-access/select", "/tenant-login"]) {
      expect(gate(req(p, { host: "go.example.test" })).status).toBe(404);
    }
  });

  it("the retired API-key sign-in answers 308 to the partner sign-in, landing on Content", () => {
    for (const p of ["/tenant-login", "/tenant-login/x"]) {
      const res = gate(req(p));
      expect(res.status).toBe(308);
      expect(res.headers.get("location")).toBe(`${ORIGIN}/auth/refresh?next=%2Fportal%2Fdashboard`);
    }
  });

  it("the studio BFF and the studio's legal pages need no session (handlers answer for themselves)", () => {
    for (const p of ["/api/tenant/me", "/api/healthz", TERMS_PATH, PRIVACY_PATH, "/assets/adventure-asia-logo.png"]) {
      const res = gate(req(p));
      expect(res.headers.get("x-middleware-next"), p).toBe("1");
      expect(res.headers.get("content-security-policy"), p).toContain("frame-ancestors 'none'");
    }
  });

  it("studio pages and access pages without a session go to /auth/refresh on this host", () => {
    for (const p of ["/portal/dashboard", "/portal-access/select"]) {
      const res = gate(req(p));
      expect(res.status).toBe(307);
      expect(res.headers.get("location")).toBe(`${ORIGIN}/auth/refresh?next=${encodeURIComponent(p)}`);
    }
  });

  it("'/' is the CRM home until WEB_CONTENT_HOME, then it lands on Content", () => {
    expect(gate(req("/")).status).toBe(307); // no session: the refresh, as before
    expect(gate(req("/")).headers.get("location")).toContain("/auth/refresh?next=%2F");
    testConfig({ ...STUDIO_ENV, WEB_CONTENT_HOME: "true" });
    const res = gate(req("/"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe(`${ORIGIN}${STUDIO_HOME_PATH}`);
  });

  it("the staff console keeps its own gate before the studio", () => {
    const res = gate(req("/staff/ops"));
    expect(res.headers.get("location") ?? "").toContain("/staff/");
  });
});

describe("studio page step (proxy -> studioGate)", () => {
  it("a session without studio cookies is exchanged once, and the page renders with the new cookies", async () => {
    const cis = fakeCis(EXCHANGED());
    vi.stubGlobal("fetch", cis);
    const res = await proxy(req("/portal/dashboard", { cookie: session() }));
    expect(res.headers.get("x-middleware-next")).toBe("1");
    const set = res.headers.getSetCookie().join("\n");
    for (const name of ["cis_tenant_token", "cis_tenant_id", "cis_partner_ref"]) expect(set).toContain(`${name}=`);
    expect(set).toContain("Secure");
    expect(res.headers.get("x-middleware-request-cookie") ?? res.headers.get("x-middleware-override-headers")).toBeTruthy();
    const [url, init] = cis.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${CIS}/auth/tenant-session`);
    expect(JSON.parse(String(init.body))).toEqual({ tenant_id: null });
  });

  it("a fresh, bound studio token verified by CIS goes through without a new exchange", async () => {
    tenantVerifyCache.delete(studioToken());
    const ref = await partnerRef("i", "sub-1");
    const token = studioToken({ exp: Math.floor(Date.now() / 1000) + 600 });
    const cis = fakeCis(EXCHANGED());
    const res = await studioGate(
      req("/portal/content", { cookie: `${session()}; cis_tenant_token=${token}; cis_partner_ref=${ref}; cis_tenant_id=${TENANT}` }),
      cis as unknown as typeof fetch,
    );
    expect(res.headers.get("x-middleware-next")).toBe("1");
    expect(res.headers.getSetCookie()).toEqual([]);
    expect(cis.mock.calls.map((c) => String(c[0]))).toEqual([`${CIS}/auth/verify-tenant`]);
  });

  it("a token made for another sign-in is never used: re-exchanged without the old agency choice", async () => {
    const cis = fakeCis(EXCHANGED());
    const res = await studioGate(
      req("/portal/content", { cookie: `${session()}; cis_tenant_token=${studioToken()}; cis_partner_ref=0123456789abcdef; cis_tenant_id=${OTHER}` }),
      cis as unknown as typeof fetch,
    );
    expect(res.headers.get("x-middleware-next")).toBe("1");
    const exchangeCall = cis.mock.calls.find((c) => String(c[0]).endsWith("/auth/tenant-session")) as unknown as [string, RequestInit];
    expect(JSON.parse(String(exchangeCall[1].body))).toEqual({ tenant_id: null });
  });

  it("an API-key token (no amr=partner) is never used, even when bound and fresh", async () => {
    const ref = await partnerRef("i", "sub-1");
    const cis = fakeCis(EXCHANGED());
    await studioGate(
      req("/portal/content", { cookie: `${session()}; cis_tenant_token=${studioToken({ amr: undefined })}; cis_partner_ref=${ref}` }),
      cis as unknown as typeof fetch,
    );
    expect(cis.mock.calls.some((c) => String(c[0]).endsWith("/auth/tenant-session"))).toBe(true);
  });

  it("several studios -> the chooser, none -> the no-access page, CIS refusal -> refresh, CIS down -> unavailable", async () => {
    const cases: [{ status: number; body: unknown }, string][] = [
      [{ status: 409, body: { detail: "choose_tenant" } }, "/portal-access/select?next=%2Fportal%2Fbrand"],
      [{ status: 403, body: { detail: "no_membership" } }, "/portal-access/none?next=%2Fportal%2Fbrand"],
      [{ status: 401, body: { detail: "invalid_token" } }, "/auth/refresh?next=%2Fportal%2Fbrand"],
      [{ status: 503, body: {} }, "/portal-access/none?next=%2Fportal%2Fbrand&reason=unavailable"],
    ];
    for (const [answer, where] of cases) {
      const res = await studioGate(req("/portal/brand", { cookie: session() }), fakeCis(answer) as unknown as typeof fetch);
      expect(res.status, where).toBe(307);
      expect(res.headers.get("location")).toBe(`${ORIGIN}${where}`);
      expect(res.headers.get("cache-control")).toBe("no-store");
    }
  });

  it("stale studio cookies of another sign-in are cleared when the new person must choose", async () => {
    const res = await studioGate(
      req("/portal/brand", { cookie: `${session()}; cis_tenant_token=${studioToken()}; cis_partner_ref=0123456789abcdef` }),
      fakeCis({ status: 409, body: { detail: "choose_tenant" } }) as unknown as typeof fetch,
    );
    const cleared = res.headers.getSetCookie().filter((c) => /Max-Age=0/i.test(c)).map((c) => c.split("=")[0]);
    expect(cleared.sort()).toEqual([...STUDIO_COOKIES].sort());
  });

  it("without CIS configured the studio fails closed", async () => {
    testConfig({});
    const res = await studioGate(req("/portal/brand", { cookie: session() }), fakeCis(EXCHANGED()) as unknown as typeof fetch);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/portal-access/none?next=%2Fportal%2Fbrand&reason=unavailable`);
  });

  it("an expired access token goes to the refresh", async () => {
    const res = await studioGate(req("/portal/brand", { cookie: session({ atExpIn: -10 }) }), fakeCis(EXCHANGED()) as unknown as typeof fetch);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/auth/refresh?next=%2Fportal%2Fbrand`);
  });
});

describe("studio BFF session (requireStudio and the routes)", () => {
  it("no session: 401 with x-aa-session expired (the shell reloads once)", async () => {
    const out = await requireStudio(req("/api/tenant/me"), fakeCis(EXCHANGED()) as unknown as typeof fetch);
    expect(out.ok).toBe(false);
    if (!out.ok) {
      expect(out.response.status).toBe(401);
      expect(out.response.headers.get("x-aa-session")).toBe("expired");
    }
  });

  it("a bound fresh token is used while the access token has expired; a missing one needs a live session", async () => {
    const ref = await partnerRef("i", "sub-1");
    const token = studioToken({ exp: Math.floor(Date.now() / 1000) + 500 });
    const ok = await requireStudio(
      req("/api/tenant/me", { cookie: `${session({ atExpIn: -10 })}; cis_tenant_token=${token}; cis_partner_ref=${ref}` }),
      fakeCis(EXCHANGED()) as unknown as typeof fetch,
    );
    expect(ok.ok).toBe(true);
    const no = await requireStudio(req("/api/tenant/me", { cookie: session({ atExpIn: -10 }) }), fakeCis(EXCHANGED()) as unknown as typeof fetch);
    expect(no.ok).toBe(false);
  });

  it("a renewed token is exchanged and set on the BFF answer; CIS refusing verification is 401", async () => {
    const cis = fakeCis(EXCHANGED(studioToken({ sub: "renewed" })));
    vi.stubGlobal("fetch", cis);
    const res = await tenantMeGet(req("/api/tenant/me", { cookie: session() }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      tenant_id: TENANT, tenant_name: "Harbour Lantern Tours", plan_tier: "growth", sign_in: "partner", content_studio_role: null,
    });
    expect(cookieHeader(res)).toContain("cis_tenant_token=");
    vi.stubGlobal("fetch", fakeCis(EXCHANGED(studioToken({ sub: "refused" })), { status: 401, body: {} }));
    expect((await tenantMeGet(req("/api/tenant/me", { cookie: session() }))).status).toBe(401);
  });

  it("the tenant proxy forwards to CIS_API_URL with the verified token and refuses bad paths", async () => {
    const token = studioToken({ sub: "proxy" });
    const cis = fakeCis(EXCHANGED(token));
    vi.stubGlobal("fetch", cis);
    const res = await tenantProxyGet(req("/api/tenant/v1/quota?x=1", { cookie: session() }), { params: Promise.resolve({ path: ["v1", "quota"] }) });
    expect(res.status).toBe(200);
    const last = cis.mock.calls.at(-1) as unknown as [string, RequestInit];
    expect(last[0]).toBe(`${CIS}/v1/quota?x=1`);
    expect((last[1].headers as Record<string, string>).Authorization).toBe(`Bearer ${token}`);
    const bad = await tenantProxyGet(req("/api/tenant/v1/..", { cookie: session() }), { params: Promise.resolve({ path: ["v1", ".."] }) });
    expect(bad.status).toBe(400);
  });

  it("sign-out clears every studio cookie and names the app's sign-out; a cross-site post is refused", async () => {
    const res = await logoutPost(req("/api/auth/tenant-logout", { method: "POST", origin: ORIGIN, cookie: session() }));
    expect(await res.json()).toEqual({ ok: true, sign_out: "/auth/sign-out" });
    const cleared = res.headers.getSetCookie().map((c) => c.split("=")[0]);
    expect(cleared.sort()).toEqual([...STUDIO_COOKIES].sort());
    const foreign = await logoutPost(req("/api/auth/tenant-logout", { method: "POST", origin: "https://evil.example.test" }));
    expect(foreign.status).toBe(403);
  });

  it("choosing a studio exchanges for that tenant and returns to the studio page", async () => {
    const cis = fakeCis(EXCHANGED());
    vi.stubGlobal("fetch", cis);
    const form = new URLSearchParams({ tenant_id: TENANT, next: "/portal/content" });
    const res = await selectPost(
      new NextRequest(`${ORIGIN}/api/auth/tenant-select`, {
        method: "POST",
        headers: { host: "partner.example.test", origin: ORIGIN, cookie: session(), "content-type": "application/x-www-form-urlencoded" },
        body: form.toString(),
      }),
    );
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/portal/content`);
    expect(JSON.parse(String((cis.mock.calls[0] as unknown as [string, RequestInit])[1].body))).toEqual({ tenant_id: TENANT });
    const signedOut = await selectPost(
      new NextRequest(`${ORIGIN}/api/auth/tenant-select`, {
        method: "POST",
        headers: { host: "partner.example.test", origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
        body: form.toString(),
      }),
    );
    expect(signedOut.headers.get("location")).toContain("/auth/refresh?next=");
  });
});

describe("app switcher (root ADR 0019 d7)", () => {
  it("maps the CRM API's /me answer", () => {
    expect(crmAccessFrom(200, { crm_entitled: true })).toBe("entitled");
    expect(crmAccessFrom(200, {})).toBe("entitled"); // an older API without the field
    expect(crmAccessFrom(200, { crm_entitled: false })).toBe("not_entitled");
    for (const s of [400, 403, 409]) expect(crmAccessFrom(s, null)).toBe("not_member");
    for (const s of [0, 401, 500, 503]) expect(crmAccessFrom(s, null)).toBe("unknown");
  });

  it("opens an entitled CRM; the others lead to the Add-ons page, labelled by their CIS state", () => {
    const open = studioApps("entitled");
    expect(open.map((a) => [a.key, a.state])).toEqual([["content", "current"], ["crm", "open"], ["planner", "request"]]);
    expect(open[1].href).toBe(CRM_HOME_PATH);
    expect(open[2].href).toBe(ADD_ONS_PATH);
    const ask = studioApps("not_entitled", { crm: "available", planner: "available" });
    expect(ask[1]).toMatchObject({ state: "request", href: ADD_ONS_PATH });
    expect(studioApps("not_member", { crm: "requested", planner: "requested" }).map((a) => a.state))
      .toEqual(["current", "requested", "requested"]);
    // the add-on is on for the agency but the person has no CRM role yet: "on", not a request
    expect(studioApps("not_member", { crm: "active", planner: "active" }).map((a) => a.state))
      .toEqual(["current", "active", "active"]);
    expect(studioApps("unknown")[1].state).toBe("open");
    expect(studioApps("not_member", UNKNOWN_ADD_ONS)[1].state).toBe("request");
  });

  it("no request is a mailto: link any more", () => {
    for (const a of [...studioApps("not_member"), ...crmApps("/portal/dashboard")]) expect(a.href.startsWith("mailto:")).toBe(false);
  });

  it("reads the CIS add-on states, and anything unexpected is unknown", () => {
    const body = { in_service: true, add_ons: [{ product: "crm", state: "requested" }, { product: "tripplanner", state: "active" }] };
    expect(addOnStatesFrom(200, body)).toEqual({ crm: "requested", planner: "active" });
    expect(addOnStatesFrom(503, null)).toEqual(UNKNOWN_ADD_ONS);
    expect(addOnStatesFrom(200, { add_ons: [{ product: "crm", state: "weird" }] })).toEqual(UNKNOWN_ADD_ONS);
    expect(addOnStatesFrom(200, "nope")).toEqual(UNKNOWN_ADD_ONS);
  });

  it("cisAddOns sends the studio token to CIS and never throws", async () => {
    const seen: string[] = [];
    const ok = vi.fn(async (url: string, init?: RequestInit) => {
      seen.push(`${url} ${new Headers(init?.headers).get("authorization")}`);
      return Response.json({ add_ons: [{ product: "crm", state: "available" }, { product: "tripplanner", state: "requested" }] });
    });
    expect(await cisAddOns(CIS, "tok", ok as unknown as typeof fetch)).toEqual({ crm: "available", planner: "requested" });
    expect(seen).toEqual([`${CIS}/v1/account/add-ons Bearer tok`]);
    expect(await cisAddOns("", "tok", ok as unknown as typeof fetch)).toEqual(UNKNOWN_ADD_ONS);
    const boom = vi.fn(async () => {
      throw new Error("down");
    });
    expect(await cisAddOns(CIS, "tok", boom as unknown as typeof fetch)).toEqual(UNKNOWN_ADD_ONS);
  });

  it("the CRM side lists the studio when allowed, itself, and the planner through the studio", () => {
    expect(crmApps("/portal/dashboard").map((a) => [a.key, a.state])).toEqual([["content", "open"], ["crm", "current"], ["planner", "request"]]);
    expect(crmApps(null).map((a) => a.key)).toEqual(["crm"]);
    expect(plannerHref("/portal/dashboard")).toBe(ADD_ONS_PATH);
    expect(plannerHref(null)).toBeNull();
  });

  it("crmAccess asks /crm/v1/me for the studio's tenant and never throws", async () => {
    const seen: string[] = [];
    const ok = vi.fn(async (_url: string, init?: RequestInit) => {
      seen.push(new Headers(init?.headers).get("x-aa-tenant-id") ?? "");
      return Response.json({ crm_entitled: false });
    });
    const s = { v: 1 as const, at: "at", atExp: 0, iss: "i", sub: "s", cid: "c", tid: OTHER, exp: 0 };
    expect(await crmAccess(cfg, s, TENANT, ok as unknown as typeof fetch)).toBe("not_entitled");
    expect(seen).toEqual([TENANT]);
    expect(await crmAccess(cfg, s, null, ok as unknown as typeof fetch)).toBe("unknown");
    const boom = vi.fn(async () => {
      throw new Error("down");
    });
    expect(await crmAccess(cfg, s, TENANT, boom as unknown as typeof fetch)).toBe("unknown");
    expect(await crmAccess(cfg, s, TENANT, (async () => Response.json({ detail: "not_a_member" }, { status: 403 })) as unknown as typeof fetch)).toBe("not_member");
  });

  it("the tenant /me names the person's studio role from the verified token (display only)", async () => {
    vi.stubGlobal("fetch", fakeCis(EXCHANGED(studioToken({ sub: "role", csr: "editor" }))));
    const res = await tenantMeGet(req("/api/tenant/me", { cookie: session() }));
    expect((await res.json()).content_studio_role).toBe("editor");
  });

  it("an invited Content member with no CRM role sees the CRM as an add-on, and the request state from CIS", async () => {
    // Done-when (root ADR 0019 d7): the invited person lands on Content and sees the CRM only with the entitlement.
    const cis = fakeCis(EXCHANGED(studioToken({ sub: "invitee", csr: "editor" })));
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/crm/v1/me")) return Response.json({ detail: "not_a_member" }, { status: 403 });
      if (url.endsWith("/v1/account/add-ons")) {
        return Response.json({ in_service: true, add_ons: [{ product: "crm", state: "requested" }, { product: "tripplanner", state: "available" }] });
      }
      return cis(url);
    }));
    const res = await appsGet(req("/api/studio/apps", { cookie: `${session()}; cis_tenant_id=${TENANT}` }));
    expect(res.status).toBe(200);
    const apps = (await res.json()).apps as { key: string; state: string; href: string }[];
    expect(apps.map((a) => [a.key, a.state, a.href])).toEqual([
      ["content", "current", "/portal/dashboard"],
      ["crm", "requested", ADD_ONS_PATH],
      ["planner", "request", ADD_ONS_PATH],
    ]);
    expect(cookieHeader(res)).toContain("cis_tenant_token="); // the renewed studio token is kept
  });

  it("GET /api/studio/apps always answers 200, signed in or not", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ crm_entitled: true })));
    const signed = await appsGet(req("/api/studio/apps", { cookie: `${session()}; cis_tenant_id=${TENANT}; cis_tenant_name=Harbour%20Lantern%20Tours` }));
    expect(signed.status).toBe(200);
    expect((await signed.json()).apps.map((a: { key: string; state: string }) => a.state)).toEqual(["current", "open", "request"]);
    const anon = await appsGet(req("/api/studio/apps"));
    expect(anon.status).toBe(200);
    expect((await anon.json()).apps[1].state).toBe("open"); // unknown keeps the link (the CRM says what is wrong)
  });
});

describe("paths, config and redirects", () => {
  it("classifies the studio's paths", () => {
    expect(isStudioPage("/portal")).toBe(true);
    expect(isStudioPage("/portal/tours")).toBe(true);
    expect(isStudioPage("/portal-access/select")).toBe(false);
    expect(isStudioAccessPage("/portal-access/none")).toBe(true);
    expect(isStudioBff("/api/tenant/me")).toBe(true);
    expect(isApiKeySignIn("/tenant-login")).toBe(true);
    expect(isApiKeySignIn("/tenant-loginx")).toBe(false);
    expect(isPublicPartnerPath(TERMS_PATH)).toBe(true);
    expect(isPublicPartnerPath("/legal/terms")).toBe(false); // the CRM terms stay behind the session
  });

  it("the CRM home is /home, and '/' still marks it active", () => {
    expect(activeSection("/")?.key).toBe("home");
    expect(activeSection(CRM_HOME_PATH)?.key).toBe("home");
  });

  it("WEB_CONTENT_HOME is off unless exactly true", () => {
    expect(loadConfig({ ...ENV }).contentHome).toBe(false);
    expect(loadConfig({ ...ENV, WEB_CONTENT_HOME: "TRUE" }).contentHome).toBe(true);
    expect(loadConfig({ ...ENV, WEB_CONTENT_HOME: "1" }).contentHome).toBe(false);
  });

  it("next.config redirects the old slugs and the old legal paths (308), in step with lib/studio/legal.ts", async () => {
    const redirects = await nextConfig.redirects!();
    const map = new Map(redirects.map((r) => [r.source, r]));
    for (const r of LEGACY_LEGAL_REDIRECTS) {
      expect(map.get(r.source)?.destination).toBe(r.destination);
      expect(map.get(r.source)?.permanent).toBe(true);
    }
    expect(map.get("/portal/t10-review")?.destination).toBe("/portal/content");
    expect(redirects.every((r) => r.permanent)).toBe(true);
    expect(map.has("/")).toBe(false); // "/" is decided by the gate (WEB_CONTENT_HOME), never a fixed redirect
  });
});
