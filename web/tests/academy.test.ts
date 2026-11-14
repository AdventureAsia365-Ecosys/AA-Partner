// AA-1105 (root ADR 0022 decisions 2 and 6): the AA Academy on its own host. Host isolation both ways (the academy host
// serves only the academy pages, its sign-in and the public verify page; the academy routes answer 404 on the partner
// and client hosts), the academy's own session (own cookies, own seal key, own client), its sign-in flow, the BFF to
// the CIS learning API and the caption proxy. Fake values only.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as signIn } from "@/app/academy/auth/sign-in/route";
import { GET as callback } from "@/app/academy/auth/callback/route";
import { GET as refresh } from "@/app/academy/auth/refresh/route";
import { POST as signOut } from "@/app/academy/auth/sign-out/route";
import { GET as bffGet, POST as bffPost } from "@/app/academy/bff/[...path]/route";
import { GET as captions } from "@/app/academy/captions/[lessonId]/[lang]/route";
import { GET as healthz } from "@/app/healthz/route";
import { ACADEMY_BFF_ROUTES, matchAcademyBff } from "@/lib/academyBff";
import { captionUrlAllowed } from "@/lib/academyCaptions";
import {
  academyCookieNames,
  academyRefreshCookie,
  academySessionCookie,
  academyTxCookie,
  isPublicAcademyPath,
  readAcademySession,
  safeAcademyNext,
} from "@/lib/academySession";
import type { WebConfig } from "@/lib/config";
import { forHost } from "@/lib/config";
import { academyHostAllowed, academyPathAllowed, classifyHost, isAcademyPath } from "@/lib/hosts";
import { sessionCookie } from "@/lib/session";
import { staffSessionCookie } from "@/lib/staffSession";
import { gate } from "@/proxy";
import { cookieHeader, fakeAccessToken, testConfig } from "./helpers";

export const ACADEMY_ENV = {
  WEB_ACADEMY_HOSTS: "academy.example.test",
  ACADEMY_COGNITO_DOMAIN: "https://staff-auth.example.test",
  ACADEMY_COGNITO_CLIENT_ID: "academyclient789",
  ACADEMY_COGNITO_CLIENT_SECRET: "eeeeeeee",
  ACADEMY_SESSION_SECRET: "uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu",
  CIS_API_URL: "https://cis-api.example.test",
};
const ACADEMY = "https://academy.example.test";
const ACADEMY_ISS = "https://cognito-idp.example.test/workforce";

let cfg: WebConfig;
beforeEach(() => {
  cfg = testConfig(ACADEMY_ENV);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function req(url: string, init: { cookie?: string; headers?: Record<string, string>; method?: string; body?: string } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", new URL(url).host);
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(url, { headers, method: init.method ?? "GET", body: init.body });
}

function academyToken(claims: Record<string, unknown> = {}) {
  return fakeAccessToken({ client_id: ACADEMY_ENV.ACADEMY_COGNITO_CLIENT_ID, iss: ACADEMY_ISS, sub: "learner-1", ...claims });
}

function academyCookie(c: WebConfig = cfg, atExp = Math.floor(Date.now() / 1000) + 600): string {
  const s = academySessionCookie(c, { at: academyToken(), atExp, iss: ACADEMY_ISS, sub: "learner-1", cid: c.academyCognitoClientId });
  return `${s.name}=${s.value}`;
}

const PAGE_CALL = { "x-aa-academy": "1", "sec-fetch-site": "same-origin" };

describe("configuration", () => {
  it("reads the academy values and keeps the academy host out of the partner and client hosts", () => {
    expect(cfg.academyAuthConfigured).toBe(true);
    expect(cfg.academyHosts).toEqual(["academy.example.test"]);
    const mixed = testConfig({ ...ACADEMY_ENV, WEB_PARTNER_HOSTS: "partner.example.test,academy.example.test", WEB_CLIENT_HOSTS: "go.example.test,academy.example.test" });
    expect(mixed.partnerHosts).toEqual(["partner.example.test"]);
    expect(mixed.clientHosts).toEqual(["go.example.test"]);
  });

  it("is off, naming what is missing, when a value is absent or reused from another session", () => {
    expect(testConfig({}).academyAuthConfigured).toBe(false);
    expect(testConfig({ ...ACADEMY_ENV, CIS_API_URL: "" }).academyProblems).toContain("CIS_API_URL");
    // Never the staff console's client or key, never the agency's key.
    expect(testConfig({ ...ACADEMY_ENV, ACADEMY_COGNITO_CLIENT_ID: "staffclient456" }).academyProblems).toContain("ACADEMY_COGNITO_CLIENT_ID");
    expect(testConfig({ ...ACADEMY_ENV, ACADEMY_SESSION_SECRET: "tttttttttttttttttttttttttttttttttttttttt" }).academyProblems).toContain("ACADEMY_SESSION_SECRET");
    expect(testConfig({ ...ACADEMY_ENV, ACADEMY_SESSION_SECRET: "ssssssssssssssssssssssssssssssssssssssss" }).academyProblems).toContain("ACADEMY_SESSION_SECRET");
    expect(testConfig({ ...ACADEMY_ENV, ACADEMY_SESSION_SECRET: "short" }).academyProblems).toContain("ACADEMY_SESSION_SECRET");
    // The agency app is unaffected by a missing academy value.
    expect(testConfig({ ...ACADEMY_ENV, ACADEMY_COGNITO_CLIENT_SECRET: "" }).authConfigured).toBe(true);
  });

  it("answers on the academy host with the academy's own origin", () => {
    expect(forHost(cfg, "academy.example.test").publicOrigin).toBe(ACADEMY);
    expect(forHost(cfg, "partner.example.test").publicOrigin).toBe("https://partner.example.test");
  });

  it("healthz says whether the academy sign-in is configured", async () => {
    const body = await healthz().json();
    expect(body).toMatchObject({ academy_auth_configured: true });
  });
});

describe("host routing", () => {
  it("classifies the academy host first and knows its paths", () => {
    expect(classifyHost("academy.example.test", cfg)).toBe("academy");
    expect(classifyHost("ACADEMY.example.test", cfg)).toBe("academy");
    expect(isAcademyPath("/academy")).toBe(true);
    expect(isAcademyPath("/academy/modules/F3")).toBe(true);
    expect(isAcademyPath("/verify/abc")).toBe(true);
    expect(isAcademyPath("/academyx")).toBe(false);
    expect(academyPathAllowed("/prefs/locale")).toBe(true);
    expect(academyPathAllowed("/_next/static/x.js")).toBe(true);
    expect(academyPathAllowed("/home")).toBe(false);
    expect(academyHostAllowed("academy.example.test", cfg)).toBe(true);
    expect(academyHostAllowed("partner.example.test", cfg)).toBe(false);
    expect(academyHostAllowed("localhost:3005", { ...cfg, environment: "local" })).toBe(true);
  });

  it("the academy host serves nothing of the agency app, the studio, the staff console, the client host or the CRM API", () => {
    for (const path of [
      "/home",
      "/pipeline",
      "/settings",
      "/auth/sign-in",
      "/auth/callback",
      "/signed-out",
      "/bff/me",
      "/bff/identity",
      "/api/tenant/me",
      "/portal/dashboard",
      "/portal-access/none",
      "/staff",
      "/staff/ops",
      "/staff/auth/sign-in",
      "/staff/bff/ops-releases",
      "/c/f/abcdefgh",
      "/trip/x",
      "/crm/health",
      "/crm/v1/me",
      "/join",
      "/kit-gallery",
      "/health",
      "/legal/privacy",
    ]) {
      expect(gate(req(`${ACADEMY}${path}`)).status, path).toBe(404);
    }
  });

  it("the partner and client hosts serve no academy route", () => {
    for (const host of ["https://partner.example.test", "https://go.example.test"]) {
      for (const path of ["/academy", "/academy/auth/sign-in", "/academy/bff/me", "/academy/signed-out", "/verify/AAAAAAAAAAAAAAAAAAAAAA"]) {
        expect(gate(req(`${host}${path}`)).status, `${host}${path}`).toBe(404);
      }
    }
  });

  it("'/' goes to the catalogue; public academy paths pass with the academy's own CSP", () => {
    const root = gate(req(`${ACADEMY}/`));
    expect(root.status).toBe(307);
    expect(root.headers.get("location")).toBe(`${ACADEMY}/academy`);
    for (const path of ["/healthz", "/academy/auth/sign-in", "/academy/auth/callback", "/academy/signed-out", "/academy/bff/me", "/academy/captions/F3-01/en", "/prefs/locale", "/robots.txt"]) {
      expect(gate(req(`${ACADEMY}${path}`)).headers.get("x-middleware-next"), path).toBe("1");
    }
    const verify = gate(req(`${ACADEMY}/verify/AAAAAAAAAAAAAAAAAAAAAA`));
    expect(verify.headers.get("x-middleware-next")).toBe("1");
    expect(verify.headers.get("referrer-policy")).toBe("no-referrer");
    expect(verify.headers.get("content-security-policy")).toContain("form-action 'self' https://staff-auth.example.test");
    expect(verify.headers.get("content-security-policy")).not.toContain("https://auth.example.test");
  });

  it("an academy page needs the academy session: none, an agency one or a staff one -> refresh", () => {
    const none = gate(req(`${ACADEMY}/academy/modules/F3?x=1`));
    expect(none.status).toBe(307);
    const target = new URL(none.headers.get("location")!);
    expect(`${target.origin}${target.pathname}`).toBe(`${ACADEMY}/academy/auth/refresh`);
    expect(target.searchParams.get("next")).toBe("/academy/modules/F3?x=1");

    const now = Math.floor(Date.now() / 1000);
    const agency = sessionCookie(cfg, { at: "tok", atExp: now + 600, iss: "i", sub: "s", cid: "c", tid: "t" });
    expect(gate(req(`${ACADEMY}/academy`, { cookie: `${agency.name}=${agency.value}` })).status).toBe(307);
    const staff = staffSessionCookie(cfg, { at: "tok", atExp: now + 600, iss: "i", sub: "s", cid: cfg.staffCognitoClientId, groups: ["crm-ops"] });
    expect(gate(req(`${ACADEMY}/academy`, { cookie: `${staff.name}=${staff.value}` })).status).toBe(307);
    // A forged academy cookie, or one sealed with another key, is no session.
    expect(gate(req(`${ACADEMY}/academy`, { cookie: `${academyCookieNames(cfg).session}=${agency.value}` })).status).toBe(307);

    const ok = gate(req(`${ACADEMY}/academy`, { cookie: academyCookie() }));
    expect(ok.headers.get("x-middleware-next")).toBe("1");
  });

  it("the academy session never opens an agency or staff page (it is not even sent there: other host)", () => {
    const res = gate(req("https://partner.example.test/home", { cookie: academyCookie() }));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("/auth/refresh");
  });

  it("an unconfigured academy sends its pages to the signed-out page", () => {
    const off = testConfig({ ...ACADEMY_ENV, ACADEMY_COGNITO_CLIENT_SECRET: "" });
    expect(off.academyAuthConfigured).toBe(false);
    const res = gate(req(`${ACADEMY}/academy`));
    expect(res.headers.get("location")).toBe(`${ACADEMY}/academy/signed-out?reason=unavailable`);
  });
});

describe("academy session", () => {
  it("opens only with the academy key and client; next stays inside /academy", () => {
    const c = academySessionCookie(cfg, { at: "a", atExp: 1, iss: "i", sub: "s", cid: cfg.academyCognitoClientId });
    expect(c.path).toBe("/academy");
    expect(c.name).toBe("__Secure-aa_academy_s");
    expect(readAcademySession(cfg, { get: () => ({ value: c.value }) })?.sub).toBe("s");
    const other = testConfig({ ...ACADEMY_ENV, ACADEMY_SESSION_SECRET: "vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv" });
    expect(readAcademySession(other, { get: () => ({ value: c.value }) })).toBeNull();
    expect(academyRefreshCookie(cfg, "rt", "s").path).toBe("/academy/auth");
    expect(safeAcademyNext("/academy/lessons/F3-01")).toBe("/academy/lessons/F3-01");
    expect(safeAcademyNext("/home")).toBe("/academy");
    expect(safeAcademyNext("//evil.example.test/academy")).toBe("/academy");
    expect(safeAcademyNext("/academy/auth/refresh")).toBe("/academy");
    expect(safeAcademyNext("/academy/bff/me")).toBe("/academy");
    expect(isPublicAcademyPath("/academy/auth/sign-in")).toBe(true);
    expect(isPublicAcademyPath("/academy")).toBe(false);
  });
});

describe("sign-in flow", () => {
  it("signs in with the academy-web client on the academy origin", () => {
    const res = signIn(req(`${ACADEMY}/academy/auth/sign-in?next=/academy/review`));
    const url = new URL(res.headers.get("location")!);
    expect(`${url.origin}${url.pathname}`).toBe("https://staff-auth.example.test/oauth2/authorize");
    expect(url.searchParams.get("client_id")).toBe("academyclient789");
    expect(url.searchParams.get("redirect_uri")).toBe(`${ACADEMY}/academy/auth/callback`);
    const tx = res.headers.getSetCookie().find((c) => c.startsWith("__Secure-aa_academy_tx="));
    expect(tx).toContain("Path=/academy/auth");
  });

  it("round-trips the callback, sets the academy cookies and refuses a wrong state or another client's token", async () => {
    const tx = academyTxCookie(cfg, { state: "st", verifier: "v".repeat(48), nonce: "n", next: "/academy/review" });
    const cookie = `${tx.name}=${tx.value}`;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: academyToken(), refresh_token: "rt-1", expires_in: 900 })));
    const ok = await callback(req(`${ACADEMY}/academy/auth/callback?code=c&state=st`, { cookie }));
    expect(ok.headers.get("location")).toBe(`${ACADEMY}/academy/review`);
    const set = cookieHeader(ok);
    expect(set).toContain("__Secure-aa_academy_s=");
    expect(set).toContain("__Secure-aa_academy_r=");

    const wrong = await callback(req(`${ACADEMY}/academy/auth/callback?code=c&state=other`, { cookie }));
    expect(wrong.headers.get("location")).toBe(`${ACADEMY}/academy/signed-out?reason=failed`);

    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: fakeAccessToken({ client_id: "staffclient456" }), expires_in: 900 })));
    const foreign = await callback(req(`${ACADEMY}/academy/auth/callback?code=c&state=st`, { cookie }));
    expect(foreign.headers.get("location")).toBe(`${ACADEMY}/academy/signed-out?reason=failed`);
  });

  it("refreshes with the academy refresh token, or signs in again", async () => {
    const r = academyRefreshCookie(cfg, "rt-1", "learner-1");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: academyToken(), expires_in: 900 })));
    const ok = await refresh(req(`${ACADEMY}/academy/auth/refresh?next=/academy/certificates`, { cookie: `${r.name}=${r.value}` }));
    expect(ok.headers.get("location")).toBe(`${ACADEMY}/academy/certificates`);
    const none = await refresh(req(`${ACADEMY}/academy/auth/refresh?next=/academy/certificates`));
    expect(none.headers.get("location")).toBe(`${ACADEMY}/academy/auth/sign-in?next=%2Facademy%2Fcertificates`);
  });

  it("signs out same-origin only, to the WORKFORCE logout with the academy's signed-out page", async () => {
    const revoke = vi.fn(async () => new Response("", { status: 200 }));
    vi.stubGlobal("fetch", revoke);
    const r = academyRefreshCookie(cfg, "rt-1", "learner-1");
    const res = await signOut(
      req(`${ACADEMY}/academy/auth/sign-out`, { method: "POST", cookie: `${r.name}=${r.value}`, headers: { origin: ACADEMY, "sec-fetch-site": "same-origin" } }),
    );
    expect(res.status).toBe(303);
    const logout = new URL(res.headers.get("location")!);
    expect(logout.searchParams.get("client_id")).toBe("academyclient789");
    expect(logout.searchParams.get("logout_uri")).toBe(`${ACADEMY}/academy/signed-out`);
    expect(revoke).toHaveBeenCalledTimes(1);
    const foreign = await signOut(req(`${ACADEMY}/academy/auth/sign-out`, { method: "POST", headers: { origin: "https://partner.example.test" } }));
    expect(foreign.status).toBe(403);
  });
});

describe("academy BFF", () => {
  const params = (...path: string[]) => ({ params: Promise.resolve({ path }) });

  it("lists every learner route of learn.http.v1 and nothing of the public or other classes", () => {
    expect(ACADEMY_BFF_ROUTES).toHaveLength(15);
    expect(matchAcademyBff("GET", "paths")).not.toBeNull();
    expect(matchAcademyBff("POST", "quiz/attempts/0f8fad5b-d9cb-469f-a165-70867728950e/submit")).not.toBeNull();
    expect(matchAcademyBff("POST", "paths")).toBeNull();
    expect(matchAcademyBff("GET", "public/v1/certificates/x")).toBeNull();
    expect(matchAcademyBff("GET", "modules/f3")).toBeNull();
  });

  it("forwards an allowed call to CIS /learn/v1 with the academy token, only the lang query", async () => {
    const upstream = vi.fn<typeof fetch>(async () => Response.json({ id: "F3-01" }));
    vi.stubGlobal("fetch", upstream);
    const res = await bffGet(req(`${ACADEMY}/academy/bff/lessons/F3-01?lang=vi&x=1`, { cookie: academyCookie(), headers: PAGE_CALL }), params("lessons", "F3-01"));
    expect(res.status).toBe(200);
    const [url, init] = upstream.mock.calls[0];
    expect(url).toBe("https://cis-api.example.test/learn/v1/lessons/F3-01?lang=vi");
    const auth = new Headers(init?.headers).get("authorization")!;
    expect(auth.startsWith("Bearer ")).toBe(true);
    const claims = JSON.parse(Buffer.from(auth.slice(7).split(".")[1], "base64url").toString("utf8"));
    expect(claims.client_id).toBe("academyclient789");
  });

  it("passes the learning API's Problem body and status through", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "retake_wait", message: "m", retry_at: "2026-11-16T10:00:00Z" }, { status: 409 })));
    const res = await bffPost(req(`${ACADEMY}/academy/bff/modules/F3/quiz/attempts`, { method: "POST", cookie: academyCookie(), headers: { ...PAGE_CALL, origin: ACADEMY } }), params("modules", "F3", "quiz", "attempts"));
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "retake_wait" });
  });

  it("refuses another host, cross-site and header-less calls, unlisted paths, traversal and missing sessions", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    expect((await bffGet(req("https://partner.example.test/academy/bff/me", { cookie: academyCookie(), headers: PAGE_CALL }), params("me"))).status).toBe(404);
    expect((await bffGet(req(`${ACADEMY}/academy/bff/me`, { cookie: academyCookie() }), params("me"))).status).toBe(403);
    expect((await bffGet(req(`${ACADEMY}/academy/bff/me`, { cookie: academyCookie(), headers: { ...PAGE_CALL, "sec-fetch-site": "same-site" } }), params("me"))).status).toBe(403);
    expect((await bffPost(req(`${ACADEMY}/academy/bff/progress`, { method: "POST", cookie: academyCookie(), headers: { ...PAGE_CALL, origin: "https://evil.example.test" } }), params("progress"))).status).toBe(403);
    expect((await bffGet(req(`${ACADEMY}/academy/bff/x`, { cookie: academyCookie(), headers: PAGE_CALL }), params("admin", "learn"))).status).toBe(404);
    expect((await bffGet(req(`${ACADEMY}/academy/bff/x`, { cookie: academyCookie(), headers: PAGE_CALL }), params("..", "me"))).status).toBe(400);
    expect((await bffGet(req(`${ACADEMY}/academy/bff/me`, { headers: PAGE_CALL }), params("me"))).status).toBe(401);
    const expired = academyCookie(cfg, Math.floor(Date.now() / 1000) - 10);
    expect((await bffGet(req(`${ACADEMY}/academy/bff/me`, { cookie: expired, headers: PAGE_CALL }), params("me"))).status).toBe(401);
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe("captions from the academy origin", () => {
  const params = (lessonId: string, lang: string) => ({ params: Promise.resolve({ lessonId, lang }) });
  const VIDEO = {
    url: "https://media-bucket.s3.us-west-1.amazonaws.com/learn-media/f3-01.mp4?X-Amz-Signature=abc",
    expires_at: "2026-11-15T10:00:00Z",
    captions: [
      { lang: "en", url: "https://media-bucket.s3.us-west-1.amazonaws.com/learn-media/f3-01.en.vtt?X-Amz-Signature=abc" },
      { lang: "vi", url: "https://media-bucket.s3.us-west-1.amazonaws.com/learn-media/f3-01.vi.vtt?X-Amz-Signature=abc" },
    ],
  };

  it("accepts only https S3 URLs", () => {
    expect(captionUrlAllowed(VIDEO.captions[0].url)).toBe(true);
    expect(captionUrlAllowed("https://s3.us-west-1.amazonaws.com/b/k.vtt")).toBe(true);
    expect(captionUrlAllowed("http://media-bucket.s3.us-west-1.amazonaws.com/k.vtt")).toBe(false);
    expect(captionUrlAllowed("https://evil.example.test/k.vtt")).toBe(false);
    expect(captionUrlAllowed("https://amazonaws.com.evil.example.test/k.vtt")).toBe(false);
    expect(captionUrlAllowed("https://u:p@media-bucket.s3.us-west-1.amazonaws.com/k.vtt")).toBe(false);
    expect(captionUrlAllowed("not a url")).toBe(false);
  });

  it("fetches the video answer, then the caption of the language, and serves it as text/vtt", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        seen.push(url);
        return url.includes("/learn/v1/") ? Response.json(VIDEO) : new Response("WEBVTT\n\n00:00.000 --> 00:01.000\nHello", { status: 200 });
      }),
    );
    const res = await captions(req(`${ACADEMY}/academy/captions/F3-01/vi`, { cookie: academyCookie() }), params("F3-01", "vi"));
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/vtt; charset=utf-8");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(await res.text()).toContain("WEBVTT");
    expect(seen).toEqual(["https://cis-api.example.test/learn/v1/lessons/F3-01/video", VIDEO.captions[1].url]);
  });

  it("refuses bad shapes, other sites, no session, a non-S3 URL and a file that is not WebVTT", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url.includes("/learn/v1/") ? Response.json(VIDEO) : new Response("<html>", { status: 200 }))));
    expect((await captions(req(`${ACADEMY}/academy/captions/x/en`, { cookie: academyCookie() }), params("x", "en"))).status).toBe(404);
    expect((await captions(req(`${ACADEMY}/academy/captions/F3-01/fr`, { cookie: academyCookie() }), params("F3-01", "fr"))).status).toBe(404);
    expect((await captions(req(`${ACADEMY}/academy/captions/F3-01/en`, { cookie: academyCookie(), headers: { "sec-fetch-site": "cross-site" } }), params("F3-01", "en"))).status).toBe(403);
    expect((await captions(req(`${ACADEMY}/academy/captions/F3-01/en`), params("F3-01", "en"))).status).toBe(401);
    expect((await captions(req("https://partner.example.test/academy/captions/F3-01/en", { cookie: academyCookie() }), params("F3-01", "en"))).status).toBe(404);
    expect((await captions(req(`${ACADEMY}/academy/captions/F3-01/en`, { cookie: academyCookie() }), params("F3-01", "en"))).status).toBe(502);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...VIDEO, captions: [{ lang: "en", url: "https://evil.example.test/x.vtt" }] })));
    expect((await captions(req(`${ACADEMY}/academy/captions/F3-01/en`, { cookie: academyCookie() }), params("F3-01", "en"))).status).toBe(502);
  });
});
