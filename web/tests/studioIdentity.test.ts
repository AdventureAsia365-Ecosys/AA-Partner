// AA-1025 (S264 hotfix): GET /bff/identity, the CRM sign-in the Content studio shows (app/bff/identity/route.ts). It
// always answers a GET with 200 (the browser logs every non-2xx as a console error on the studio pages), never echoes
// more of the API /me answer than display_name and member_role, and leaves /bff/me (401 / 409) as it was.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DELETE as identityDelete, GET as identityGet, PATCH as identityPatch, POST as identityPost, PUT as identityPut } from "@/app/bff/identity/route";
import { GET as bffGet } from "@/app/bff/[...path]/route";
import { pickIdentity } from "@/lib/crmIdentity";
import { refreshCookie, sessionCookie } from "@/lib/session";
import { config as proxyConfig, gate } from "@/proxy";
import { ENV, fakeAccessToken, testConfig } from "./helpers";

const ORIGIN = "https://partner.example.test";
const AGENCY = "3f6a2b1c-8d4e-4f5a-9b6c-7d8e9f0a1b2c";
let cfg = testConfig();

beforeEach(() => {
  cfg = testConfig();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function request(path: string, init: { method?: string; cookie?: string; host?: string; headers?: Record<string, string> } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", init.host ?? "partner.example.test");
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(`${ORIGIN}${path}`, { method: init.method ?? "GET", headers });
}

function signedIn(tid: string | null = AGENCY, atExpIn = 1200, em?: string): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + atExpIn, iss: "i", sub: "sub-1", cid: "c", tid, ...(em ? { em } : {}) });
  const r = refreshCookie(cfg, "rt-1", "sub-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

const ME = {
  tenant_id: AGENCY,
  member_id: "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
  member_role: "agent",
  display_name: "Lantern Bay Trips",
  default_locale: "en-AU",
};

describe("GET /bff/identity", () => {
  it("signed in to an agency: forwards to the API /me with the session's token and agency, answers only two fields", async () => {
    const upstream = vi.fn(async () => Response.json(ME));
    vi.stubGlobal("fetch", upstream);
    const res = await identityGet(
      request("/bff/identity", { cookie: signedIn(), headers: { "x-aa-tenant-id": "tenant-evil", authorization: "Bearer stolen" } }),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ kind: "agency", display_name: "Lantern Bay Trips", member_role: "agent" });
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(upstream).toHaveBeenCalledTimes(1);
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${ENV.CRM_API_ORIGIN}/crm/v1/me`);
    expect(init.method).toBe("GET");
    const h = init.headers as Headers;
    expect(h.get("x-aa-tenant-id")).toBe(AGENCY);
    expect(h.get("authorization")).toMatch(/^Bearer /);
    expect(h.get("authorization")).not.toBe("Bearer stolen");
  });

  it("never echoes other /me fields (ids, locale, anything the API adds later)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...ME, email: "kim@example.test", extra: { a: 1 } })));
    const body = await (await identityGet(request("/bff/identity", { cookie: signedIn() }))).json();
    expect(Object.keys(body).sort()).toEqual(["display_name", "kind", "member_role"]);
    expect(JSON.stringify(body)).not.toContain(AGENCY);
    expect(JSON.stringify(body)).not.toContain("kim@example.test");
  });

  it("adds the person's own e-mail from the session (AA-1036), never the one the API answers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...ME, email: "kim@example.test" })));
    const body = await (await identityGet(request("/bff/identity", { cookie: signedIn(AGENCY, 1200, "mira.solano@example.test") }))).json();
    expect(body).toEqual({ kind: "agency", display_name: "Lantern Bay Trips", member_role: "agent", email: "mira.solano@example.test" });
    const none = await (await identityGet(request("/bff/identity", { cookie: signedIn(null, 1200, "mira.solano@example.test") }))).json();
    expect(none).toEqual({ kind: "no_agency", email: "mira.solano@example.test" });
  });

  it("no session, or an expired access token: 200 signed_out, the API is not called", async () => {
    const upstream = vi.fn(async () => Response.json(ME));
    vi.stubGlobal("fetch", upstream);
    for (const cookie of [undefined, signedIn(AGENCY, -60), "aa_crm_session=garbage"]) {
      const res = await identityGet(request("/bff/identity", { cookie }));
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ kind: "signed_out" });
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("signed in without an agency: 200 no_agency, the API is not called", async () => {
    const upstream = vi.fn(async () => Response.json(ME));
    vi.stubGlobal("fetch", upstream);
    const res = await identityGet(request("/bff/identity", { cookie: signedIn(null) }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ kind: "no_agency" });
    expect(upstream).not.toHaveBeenCalled();
  });

  it("any upstream failure: 200 unknown (error status, unreachable, timeout, not JSON)", async () => {
    const answers: (() => Promise<Response>)[] = [
      async () => Response.json({ detail: "forbidden" }, { status: 403 }),
      async () => Response.json({ detail: "boom" }, { status: 500 }),
      async () => new Response(null, { status: 302, headers: { location: "https://elsewhere.example.test" } }),
      async () => {
        throw new TypeError("fetch failed");
      },
      async () => {
        throw new DOMException("timed out", "TimeoutError");
      },
      async () => new Response("<html>oops</html>", { status: 200 }),
      async () => Response.json(["not", "an", "object"]),
    ];
    for (const answer of answers) {
      vi.stubGlobal("fetch", vi.fn(answer));
      const res = await identityGet(request("/bff/identity", { cookie: signedIn() }));
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ kind: "unknown" });
    }
  });

  it("a Host other than the partner host: 404 before any cookie is read or the API is called", async () => {
    const upstream = vi.fn(async () => Response.json(ME));
    vi.stubGlobal("fetch", upstream);
    for (const host of ["go.example.test", "evil.example.test"]) {
      const res = await identityGet(request("/bff/identity", { cookie: signedIn(), host }));
      expect(res.status).toBe(404);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("other methods: 405 with Allow: GET (404 on a foreign host, 403 cross-site)", async () => {
    const upstream = vi.fn(async () => Response.json(ME));
    vi.stubGlobal("fetch", upstream);
    for (const [method, handler] of [["POST", identityPost], ["PUT", identityPut], ["PATCH", identityPatch], ["DELETE", identityDelete]] as const) {
      const res = await handler(request("/bff/identity", { method, cookie: signedIn() }));
      expect(res.status, method).toBe(405);
      expect(res.headers.get("allow")).toBe("GET");
      expect((await handler(request("/bff/identity", { method, cookie: signedIn(), host: "go.example.test" }))).status).toBe(404);
      const crossSite = request("/bff/identity", { method, cookie: signedIn(), headers: { origin: "https://evil.example.test" } });
      expect((await handler(crossSite)).status, `${method} cross-site`).toBe(403);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("/bff/me keeps its 401 / 409 for the CRM web", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(ME)));
    const p = { params: Promise.resolve({ path: ["me"] }) };
    expect((await bffGet(request("/bff/me"), p)).status).toBe(401);
    expect((await bffGet(request("/bff/me", { cookie: signedIn(null) }), p)).status).toBe(409);
  });

  it("the proxy sends /bff/identity through on the partner host without a session (no sign-in redirect)", () => {
    expect(new RegExp(`^${proxyConfig.matcher[0]}$`).test("/bff/identity")).toBe(true);
    const res = gate(new NextRequest(`${ORIGIN}/bff/identity`, { headers: { host: "partner.example.test" } }));
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });
});

describe("pickIdentity", () => {
  it("keeps two short strings, null for anything missing or not a string", () => {
    expect(pickIdentity({ display_name: "  Lantern Bay Trips ", member_role: "owner" })).toEqual({
      kind: "agency",
      display_name: "Lantern Bay Trips",
      member_role: "owner",
    });
    expect(pickIdentity({ display_name: null, member_role: 7 })).toEqual({ kind: "agency", display_name: null, member_role: null });
    expect(pickIdentity({ display_name: "x".repeat(500) })).toMatchObject({ display_name: "x".repeat(200) });
    for (const bad of [null, "text", 3, ["a"]]) expect(pickIdentity(bad)).toEqual({ kind: "unknown" });
  });
});
