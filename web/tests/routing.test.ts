// Host routing + the session gate (proxy.ts), safe return paths, the BFF allow-list and nav visibility.
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it } from "vitest";
import { matchBff, upstreamHeaders, BFF_ROUTES } from "@/lib/bff";
import { classifyHost, clientPathAllowed, hasRefreshMarker, isPublicPartnerPath, safeNext, withRefreshMarker } from "@/lib/hosts";
import { activeSection, contentStudioHref, isRole, sectionByKey, visibleSections } from "@/lib/nav";
import { cookieNames, sessionCookie } from "@/lib/session";
import { contentSecurityPolicy, gate, proxy } from "@/proxy";
import { testConfig } from "./helpers";

let cfg = testConfig();
beforeEach(() => {
  cfg = testConfig();
});

function req(url: string, init: { host?: string; cookie?: string; headers?: Record<string, string> } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", init.host ?? new URL(url).host);
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(url, { headers });
}

function validCookie(): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: "tok", atExp: now + 600, iss: "i", sub: "s", cid: "c", tid: "t" });
  return `${s.name}=${s.value}`;
}

describe("host classification", () => {
  it("knows partner, client and unknown hosts; localhost only in local / test", () => {
    expect(classifyHost("partner.example.test", cfg)).toBe("partner");
    expect(classifyHost("GO.example.test", cfg)).toBe("client");
    expect(classifyHost("evil.example.test", cfg)).toBe("unknown");
    expect(classifyHost(null, cfg)).toBe("unknown");
    expect(classifyHost("localhost:3005", cfg)).toBe("unknown");
    expect(classifyHost("localhost:3005", { ...cfg, environment: "local" })).toBe("partner");
    expect(clientPathAllowed("/c/inquiry")).toBe(true);
    expect(clientPathAllowed("/settings")).toBe(false);
    expect(isPublicPartnerPath("/auth/callback")).toBe(true);
    expect(isPublicPartnerPath("/healthz")).toBe(true);
    expect(isPublicPartnerPath("/healthzz")).toBe(false);
    expect(isPublicPartnerPath("/pipeline")).toBe(false);
  });
});

describe("proxy", () => {
  it("answers /healthz for any Host (ALB health checks use the task IP)", () => {
    expect(gate(req("http://10.0.0.5:3000/healthz")).headers.get("x-middleware-next")).toBe("1");
  });

  it("404s unknown hosts and non-client paths on the client host", () => {
    expect(gate(req("https://evil.example.test/")).status).toBe(404);
    expect(gate(req("https://go.example.test/settings")).status).toBe(404);
    expect(gate(req("https://go.example.test/c/inquiry")).headers.get("x-middleware-next")).toBe("1");
    expect(gate(req("https://go.example.test/robots.txt")).headers.get("x-middleware-next")).toBe("1");
    expect(gate(req("https://partner.example.test/c/inquiry")).status).toBe(404);
  });

  it("sends a partner page without a valid session to /auth/refresh with the return path", () => {
    const res = gate(req("https://partner.example.test/pipeline?view=board"));
    expect(res.status).toBe(307);
    const loc = new URL(res.headers.get("location")!);
    expect(loc.origin + loc.pathname).toBe("https://partner.example.test/auth/refresh");
    expect(loc.searchParams.get("next")).toBe("/pipeline?view=board");
    const forged = gate(req("https://partner.example.test/pipeline", { cookie: `${cookieNames(cfg).session}=forged` }));
    expect(forged.status).toBe(307);
  });

  it("lets a valid session through, overwriting forged internal headers, with the CSP", () => {
    const res = gate(
      req("https://partner.example.test/clients", { cookie: validCookie(), headers: { "x-aa-path": "/evil" } }),
    );
    expect(res.headers.get("x-middleware-next")).toBe("1");
    expect(res.headers.get("x-middleware-request-x-aa-path")).toBe("/clients");
    expect(res.headers.get("content-security-policy")).toBe(contentSecurityPolicy(cfg.cognitoDomain));
    expect(contentSecurityPolicy("https://auth.example.test")).toContain("form-action 'self' https://auth.example.test");
    expect(contentSecurityPolicy("https://auth.example.test")).toContain("frame-ancestors 'none'");
  });

  it("passes public paths and the BFF without a session (the BFF answers 401 itself)", () => {
    for (const path of ["/auth/sign-in", "/signed-out", "/health", "/bff/me", "/prefs/locale", "/robots.txt", "/icon.svg"]) {
      expect(gate(req(`https://partner.example.test${path}`)).headers.get("x-middleware-next"), path).toBe("1");
    }
  });
});

describe("safeNext", () => {
  it("keeps same-origin paths and refuses everything else", () => {
    expect(safeNext("/pipeline?x=1")).toBe("/pipeline?x=1");
    for (const bad of ["https://evil.test", "//evil.test", "/\\evil.test", "/a\\b", "pipeline", "/auth/callback", "/bff/me", "/x\u0000", null, "/" + "a".repeat(600)]) {
      expect(safeNext(bad as string | null)).toBe("/");
    }
  });
});

describe("refresh marker (no page -> /auth/refresh loop)", () => {
  it("adds the one-shot marker once and detects it", () => {
    expect(withRefreshMarker("/pipeline")).toBe("/pipeline?_rf=1");
    expect(withRefreshMarker("/pipeline?view=board")).toBe("/pipeline?view=board&_rf=1");
    expect(withRefreshMarker("/pipeline?_rf=1")).toBe("/pipeline?_rf=1");
    expect(hasRefreshMarker("/pipeline")).toBe(false);
    expect(hasRefreshMarker("/pipeline?x_rf=1")).toBe(false);
    expect(hasRefreshMarker(withRefreshMarker("/pipeline?view=board"))).toBe(true);
    expect(safeNext(withRefreshMarker("/settings"))).toBe("/settings?_rf=1");
  });
});

describe("BFF allow-list", () => {
  it("matches only listed (method, path) pairs", () => {
    expect(matchBff("get", "me")?.tenant).toBe(true);
    expect(matchBff("GET", "memberships")?.tenant).toBe(false);
    expect(matchBff("POST", "me")).toBeNull();
    expect(matchBff("GET", "me/extra")).toBeNull();
    expect(matchBff("GET", "xme")).toBeNull();
    expect(BFF_ROUTES.every((r) => r.pattern.source.startsWith("^") && r.pattern.source.endsWith("$"))).toBe(true);
  });

  it("builds upstream headers from the session only", () => {
    const h = upstreamHeaders("tok", "tenant-1", "application/json; charset=utf-8", "rid");
    expect(h.get("authorization")).toBe("Bearer tok");
    expect(h.get("x-aa-tenant-id")).toBe("tenant-1");
    expect(h.get("content-type")).toBe("application/json");
    expect(upstreamHeaders("tok", null, "text/html", "rid").get("x-aa-tenant-id")).toBeNull();
    expect(upstreamHeaders("tok", null, "text/html", "rid").get("content-type")).toBeNull();
  });
});

describe("navigation", () => {
  it("hides Commissions and Marketing from the assistant", () => {
    const assistant = visibleSections("assistant").map((s) => s.key);
    expect(assistant).not.toContain("commissions");
    expect(assistant).not.toContain("marketing");
    expect(visibleSections("owner").map((s) => s.key)).toContain("commissions");
    expect(sectionByKey("pipeline")?.card).toBe("AA-936");
    expect(sectionByKey("home")).toBeNull();
    expect(sectionByKey("nope")).toBeNull();
    expect(activeSection("/")?.key).toBe("home");
    expect(activeSection("/clients/123")?.key).toBe("clients");
    expect(contentStudioHref("https://portal.example.test")).toBe("https://portal.example.test/portal/dashboard");
    expect(contentStudioHref("")).toBeNull();
    expect(isRole("owner")).toBe(true);
    expect(isRole("admin")).toBe(false);
  });
});
