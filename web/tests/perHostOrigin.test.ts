// AA-979 P2b (critic C4) — the web answers BOTH agency hosts during the window (partner-dev. and portal.), each with its
// own public origin: redirects, the OAuth redirect / logout URIs and the same-origin write guard stay on the host the
// person is on. A Host outside WEB_PARTNER_HOSTS keeps WEB_PUBLIC_ORIGIN; the bind address is never used.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as signIn } from "@/app/auth/sign-in/route";
import { POST as signOut } from "@/app/auth/sign-out/route";
import { POST as setLocale } from "@/app/prefs/locale/route";
import { forHost, requestConfig } from "@/lib/config";
import { CONTENT_STUDIO_PATH, contentStudioHref } from "@/lib/nav";
import { refreshCookie, sessionCookie } from "@/lib/session";
import { refuseForeignWrite } from "@/lib/writeGuard";
import { gate } from "@/proxy";
import { fakeAccessToken, testConfig } from "./helpers";

const PARTNER = "https://partner.example.test";
const AGENCY = "https://portal.example.test";
const BIND = "http://0.0.0.0:3000";
const TWO_HOSTS = { WEB_PARTNER_HOSTS: "partner.example.test,portal.example.test" };

let cfg = testConfig(TWO_HOSTS);
beforeEach(() => {
  cfg = testConfig(TWO_HOSTS);
});
afterEach(() => vi.unstubAllGlobals());

function behindAlb(host: string, p: string, init: { method?: string; headers?: Record<string, string>; cookie?: string; body?: BodyInit } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", host);
  headers.set("x-forwarded-proto", "http");
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(`${BIND}${p}`, { method: init.method ?? "POST", headers, body: init.body });
}

describe("forHost", () => {
  it("gives each listed agency host its own https origin", () => {
    expect(forHost(cfg, "portal.example.test").publicOrigin).toBe(AGENCY);
    expect(forHost(cfg, "PORTAL.example.test").publicOrigin).toBe(AGENCY);
    expect(forHost(cfg, "partner.example.test").publicOrigin).toBe(PARTNER);
  });

  it("keeps WEB_PUBLIC_ORIGIN for any other Host (task IP, unknown name, missing)", () => {
    for (const host of ["10.0.1.23:3000", "evil.example.test", "", null]) {
      expect(forHost(cfg, host).publicOrigin).toBe(PARTNER);
    }
  });

  it("does not touch a local http configuration", () => {
    const local = testConfig({ ENVIRONMENT: "local", WEB_PUBLIC_ORIGIN: "http://localhost:3005", WEB_PARTNER_HOSTS: "localhost:3005,portal.example.test" });
    expect(forHost(local, "portal.example.test").publicOrigin).toBe("http://localhost:3005");
  });

  it("is what requestConfig reads from the Host header", () => {
    expect(requestConfig(behindAlb("portal.example.test", "/")).publicOrigin).toBe(AGENCY);
  });
});

describe("sign-in and sign-out stay on the agency host", () => {
  it("sends portal. sign-in to Cognito with portal.'s callback", () => {
    const res = signIn(behindAlb("portal.example.test", "/auth/sign-in?next=/clients", { method: "GET" }));
    const url = new URL(res.headers.get("location")!);
    expect(url.searchParams.get("redirect_uri")).toBe(`${AGENCY}/auth/callback`);
    const partner = signIn(behindAlb("partner.example.test", "/auth/sign-in", { method: "GET" }));
    expect(new URL(partner.headers.get("location")!).searchParams.get("redirect_uri")).toBe(`${PARTNER}/auth/callback`);
  });

  it("accepts a same-origin sign-out on portal. and logs out to portal.'s signed-out page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 200 })));
    const now = Math.floor(Date.now() / 1000);
    const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid: null });
    const r = refreshCookie(cfg, "rt", "sub-1");
    const res = await signOut(
      behindAlb("portal.example.test", "/auth/sign-out", {
        headers: { origin: AGENCY, "sec-fetch-site": "same-origin" },
        cookie: `${s.name}=${s.value}; ${r.name}=${r.value}`,
      }),
    );
    expect(res.status).toBe(303);
    expect(new URL(res.headers.get("location")!).searchParams.get("logout_uri")).toBe(`${AGENCY}/signed-out`);
  });

  it("refuses a portal. origin posting to partner-dev. and the reverse (each host is its own origin)", () => {
    const toPartner = behindAlb("partner.example.test", "/x", { headers: { origin: AGENCY } });
    expect(refuseForeignWrite(requestConfig(toPartner), toPartner)?.status).toBe(403);
    const toAgency = behindAlb("portal.example.test", "/x", { headers: { origin: PARTNER } });
    expect(refuseForeignWrite(requestConfig(toAgency), toAgency)?.status).toBe(403);
    const same = behindAlb("portal.example.test", "/x", { headers: { origin: AGENCY } });
    expect(refuseForeignWrite(requestConfig(same), same)).toBeNull();
  });

  it("redirects after a write on the host it came from", async () => {
    const f = new FormData();
    f.set("locale", "en-GB");
    f.set("next", "/tasks");
    const res = await setLocale(behindAlb("portal.example.test", "/prefs/locale", { headers: { origin: AGENCY }, body: f }));
    expect(res.headers.get("location")).toBe(`${AGENCY}/tasks`);
  });
});

describe("proxy gate on the agency host", () => {
  it("sends a page without a session to portal.'s own /auth/refresh", () => {
    const res = gate(behindAlb("portal.example.test", "/clients", { method: "GET" }));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe(`${AGENCY}/auth/refresh?next=%2Fclients`);
  });
});

describe("Content studio link", () => {
  it("is same-origin on the agency host and absolute from partner-dev.", () => {
    expect(contentStudioHref("https://portal.example.test", AGENCY)).toBe(CONTENT_STUDIO_PATH);
    expect(contentStudioHref("https://portal.example.test", PARTNER)).toBe("https://portal.example.test/portal/dashboard");
    expect(contentStudioHref("https://portal.example.test")).toBe("https://portal.example.test/portal/dashboard");
    expect(contentStudioHref("", AGENCY)).toBeNull();
  });
});
