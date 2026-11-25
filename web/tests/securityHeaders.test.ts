// AA-1111 (S284, ZAP baseline): the full CSP (lib/securityHeaders.ts), sent with a per-request nonce by proxy.ts on every
// response it handles (404s and sign-in redirects included), the static header set of next.config.ts and the nonce on
// the root layout's inline scripts. Hosts are invented.
import fs from "node:fs";
import path from "node:path";
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it } from "vitest";
import nextConfig from "@/next.config";
import {
  MAPBOX_HOSTS,
  STATIC_SECURITY_HEADERS,
  contentSecurityPolicy,
  newNonce,
  sentryOrigin,
} from "@/lib/securityHeaders";
import { gate, nonceFor, proxy } from "@/proxy";
import { testConfig } from "./helpers";

beforeEach(() => {
  testConfig();
});

function directives(csp: string): Map<string, string[]> {
  return new Map(
    csp.split(";").map((d) => {
      const [name, ...src] = d.trim().split(/\s+/);
      return [name, src] as [string, string[]];
    }),
  );
}

function req(url: string) {
  const headers = new Headers();
  headers.set("host", new URL(url).host);
  return new NextRequest(url, { headers });
}

describe("contentSecurityPolicy", () => {
  it("allows scripts by nonce only, never 'unsafe-inline'", () => {
    const d = directives(contentSecurityPolicy({ nonce: "abc==" }));
    expect(d.get("script-src")).toEqual(["'self'", "'nonce-abc=='", "'strict-dynamic'"]);
    expect(directives(contentSecurityPolicy()).get("script-src")).toEqual(["'self'"]);
    expect(directives(contentSecurityPolicy({ nonce: "n", dev: true })).get("script-src")).toContain("'unsafe-eval'");
  });

  it("names every fetch directive, none a bare wildcard", () => {
    const d = directives(contentSecurityPolicy({ nonce: "n" }));
    for (const name of ["default-src", "script-src", "style-src", "font-src", "img-src", "media-src", "connect-src",
      "frame-src", "worker-src", "object-src", "base-uri", "form-action", "frame-ancestors"]) {
      expect(d.has(name), name).toBe(true);
      expect(d.get(name), name).not.toContain("*");
    }
    expect(d.get("default-src")).toEqual(["'self'"]);
    expect(d.get("frame-ancestors")).toEqual(["'none'"]);
    // The CI standalone smoke greps "content-security-policy: frame-ancestors 'none'".
    expect(contentSecurityPolicy({ nonce: "n" }).startsWith("frame-ancestors 'none'; ")).toBe(true);
    expect(d.get("object-src")).toEqual(["'none'"]);
  });

  it("adds form-action, frame-ancestors and connect-src origins, skipping empty ones", () => {
    const d = directives(contentSecurityPolicy({
      formAction: ["https://auth.example.test", "", null],
      frameAncestors: "'self' https://www.agency.example",
      connectSrc: ["https://ingest.example.test", undefined],
    }));
    expect(d.get("form-action")).toEqual(["'self'", "https://auth.example.test"]);
    expect(d.get("frame-ancestors")).toEqual(["'self'", "https://www.agency.example"]);
    expect(d.get("connect-src")).toEqual(["'self'", ...MAPBOX_HOSTS, "https://ingest.example.test"]);
  });

  it("lets the builder map load: Mapbox API, tiles and telemetry, blob workers (AA-948)", () => {
    const d = directives(contentSecurityPolicy({ nonce: "n" }));
    for (const host of ["https://api.mapbox.com", "https://*.tiles.mapbox.com", "https://events.mapbox.com"]) {
      expect(d.get("connect-src")).toContain(host);
    }
    expect(d.get("worker-src")).toContain("blob:");
    expect(d.get("img-src")).toEqual(expect.arrayContaining(["data:", "blob:"]));
  });

  it("keeps the https origin of a Sentry DSN only", () => {
    expect(sentryOrigin("https://key@o1.ingest.example.test/42")).toBe("https://o1.ingest.example.test");
    expect(sentryOrigin("http://key@ingest.example.test/7")).toBeNull();
    expect(sentryOrigin("not a url")).toBeNull();
    expect(sentryOrigin("  ")).toBeNull();
    expect(sentryOrigin(undefined)).toBeNull();
  });

  it("makes a fresh 16-byte nonce", () => {
    const a = newNonce();
    expect(a).not.toBe(newNonce());
    expect(Buffer.from(a, "base64")).toHaveLength(16);
  });
});

describe("proxy CSP", () => {
  it("one nonce per request, a new one for the next request", () => {
    const r = req("https://partner.example.test/health");
    expect(nonceFor(r)).toBe(nonceFor(r));
    expect(nonceFor(req("https://partner.example.test/health"))).not.toBe(nonceFor(r));
  });

  it("a passed page carries the nonce on the response and hands it to the page", () => {
    const r = req("https://partner.example.test/health");
    const res = gate(r);
    const nonce = nonceFor(r);
    expect(res.headers.get("content-security-policy")).toContain(`'nonce-${nonce}'`);
    expect(res.headers.get("x-middleware-request-x-nonce")).toBe(nonce);
    expect(res.headers.get("x-middleware-request-content-security-policy")).toContain(`'nonce-${nonce}'`);
  });

  it("a forged x-nonce from the browser is replaced", () => {
    const headers = new Headers({ host: "partner.example.test", "x-nonce": "forged" });
    const r = new NextRequest("https://partner.example.test/health", { headers });
    expect(gate(r).headers.get("x-middleware-request-x-nonce")).toBe(nonceFor(r));
  });

  it("a 404 and a sign-in redirect carry the CSP too", async () => {
    for (const url of ["https://unknown.example.test/x", "https://partner.example.test/clients"]) {
      const res = await proxy(req(url));
      expect([404, 307], url).toContain(res.status);
      const csp = res.headers.get("content-security-policy") ?? "";
      expect(csp, url).toContain("default-src 'self'");
      expect(csp, url).toContain("frame-ancestors 'none'");
      expect(csp, url).not.toMatch(/script-src[^;]*'unsafe-inline'/);
    }
  });
});

describe("static headers and the layout", () => {
  it("next.config.ts sends the static set on every path and the CSP on the paths the proxy skips", async () => {
    const rules = (await nextConfig.headers?.()) ?? [];
    const all = rules.find((r) => r.source === "/(.*)");
    expect(all?.headers).toEqual(STATIC_SECURITY_HEADERS);
    const keys = STATIC_SECURITY_HEADERS.map((h) => h.key);
    expect(keys).toEqual(expect.arrayContaining(["Strict-Transport-Security", "X-Content-Type-Options",
      "Cross-Origin-Opener-Policy", "Permissions-Policy", "Referrer-Policy", "X-Frame-Options"]));
    expect(STATIC_SECURITY_HEADERS.find((h) => h.key === "Strict-Transport-Security")?.value).not.toContain("preload");
    for (const source of ["/_next/:path*", "/favicon.ico", "/bff/mail/imports", "/bff/mail/imports/:path*"]) {
      const rule = rules.find((r) => r.source === source);
      expect(rule?.headers[0].key, source).toBe("Content-Security-Policy");
      expect(rule?.headers[0].value, source).toContain("frame-ancestors 'none'");
    }
  });

  it("every inline script of the root layout carries the nonce", () => {
    const layout = fs.readFileSync(path.join(__dirname, "..", "app", "layout.tsx"), "utf8");
    expect(layout).toContain("(await headers()).get(NONCE_HEADER)");
    expect(layout.split("<script").length).toBe(layout.split("<script nonce={nonce}").length);
  });
});
