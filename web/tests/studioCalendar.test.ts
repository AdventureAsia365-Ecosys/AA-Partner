// S294 (AA-1011 follow-up) — the studio's Calendar card reads and writes through the studio's own route handler
// (app/api/studio/calendar), with the studio's tenant, never the CRM's /bff: a studio session picks no CRM agency, so
// /bff/calendar/connections answered 409 `no_agency_selected` on page load and Deploy Web Dev's studio smoke rolled the
// web back. The read is display only and answers 200 "not available" for a person the CRM cannot serve here.
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { refreshCookie, sessionCookie } from "@/lib/session";
import { BFF_ROUTES, matchBff, SERVER_ONLY_ROUTES } from "@/lib/bff";
import {
  calendarRoute,
  NO_CRM_CALENDAR,
  STUDIO_CALENDAR_API,
  studioCalendarUrl,
  viewAnswer,
  writeAnswer,
} from "@/lib/studio/calendar";
import { ENV, fakeAccessToken, testConfig } from "./helpers";

const STUDIO_TENANT = "5d0c3e2a-1b4f-4c6d-9e8f-7a6b5c4d3e2f";
const CONNECTION = "00000000-0000-4000-8000-00000000c011";
const ORIGIN = "https://partner.example.test";

const studio = vi.hoisted(() => ({ ok: true }));
vi.mock("@/lib/studio/auth-server", async (orig) => {
  const real = await orig<typeof import("@/lib/studio/auth-server")>();
  const { requestConfig } = await import("@/lib/config");
  const { NextResponse } = await import("next/server");
  return {
    ...real,
    requireStudio: vi.fn(async (req: NextRequest) =>
      studio.ok
        ? { ok: true, tenantId: STUDIO_TENANT, name: "Lantern Bay Trips", planTier: "starter", token: "studio-token", renewed: [], cfg: requestConfig(req) }
        : { ok: false, response: NextResponse.json({ detail: "studio_session_required" }, { status: 401 }) },
    ),
  };
});
const { GET, POST } = await import("@/app/api/studio/calendar/[[...path]]/route");
const { GET: bffGet } = await import("@/app/bff/[...path]/route");

let cfg: ReturnType<typeof testConfig>;
beforeEach(() => {
  cfg = testConfig();
  studio.ok = true;
});
afterEach(() => {
  vi.unstubAllGlobals();
});

/** A studio session: signed in, no CRM agency picked (tid null), exactly what the portal smoke's person has. */
function signedIn(atExpIn = 1200): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + atExpIn, iss: "i", sub: "sub-1", cid: "c", tid: null });
  const r = refreshCookie(cfg, "rt-1", "sub-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

function request(p: string, init: { method?: string; body?: unknown; cookie?: string; origin?: string } = {}) {
  const headers = new Headers({ host: "partner.example.test", cookie: init.cookie ?? signedIn() });
  if (init.body !== undefined) headers.set("content-type", "application/json");
  if (init.method && init.method !== "GET") headers.set("origin", init.origin ?? ORIGIN);
  return new NextRequest(`${ORIGIN}${p}`, {
    method: init.method ?? "GET",
    headers,
    body: init.body === undefined ? undefined : typeof init.body === "string" ? init.body : JSON.stringify(init.body),
  });
}

function params(...segments: string[]) {
  return { params: Promise.resolve({ path: segments }) };
}

const VIEW = { connections: [], providers: { google: true, microsoft: false }, kinds: ["departure", "task"] };

describe("calendar routes of the studio handler", () => {
  it.each([
    ["GET", [], "GET", "calendar/connections", true],
    ["get", [], "GET", "calendar/connections", true],
    ["POST", ["oauth", "start"], "POST", "calendar/connections/oauth/start", false],
    ["POST", ["oauth", "complete"], "POST", "calendar/connections/oauth/complete", false],
    ["POST", [CONNECTION, "disconnect"], "POST", `calendar/connections/${CONNECTION}/disconnect`, false],
  ] as const)("%s /%j -> %s %s", (method, segments, upMethod, sub, view) => {
    const route = calendarRoute(method, segments);
    expect(route).toMatchObject({ method: upMethod, sub, view });
    // Every upstream path is a server-only line of the allow-list, never a browser /bff one.
    expect(matchBff(upMethod, sub, SERVER_ONLY_ROUTES)?.card).toBe("AA-1011");
    expect(matchBff(upMethod, sub, BFF_ROUTES)).toBeNull();
  });

  it.each([
    ["POST", []],
    ["GET", ["oauth", "start"]],
    ["DELETE", []],
    ["POST", ["not-a-uuid", "disconnect"]],
    ["POST", [CONNECTION.toUpperCase(), "disconnect"]],
    ["POST", [CONNECTION, "disconnect", "x"]],
    ["POST", ["..", "oauth", "start"]],
  ] as const)("%s /%j is not a route", (method, segments) => {
    expect(calendarRoute(method, segments)).toBeNull();
  });

  it("builds the card's URLs", () => {
    expect(studioCalendarUrl("")).toBe(STUDIO_CALENDAR_API);
    expect(studioCalendarUrl("oauth/start")).toBe("/api/studio/calendar/oauth/start");
    expect(studioCalendarUrl(`${CONNECTION}/disconnect`)).toBe(`/api/studio/calendar/${CONNECTION}/disconnect`);
  });
});

describe("answers", () => {
  it("the read answers 200 for a person the CRM cannot serve here, 401 to sign in again, 503 for an outage", () => {
    expect(viewAnswer({ ok: true, status: 200, data: VIEW })).toEqual({ status: 200, body: { ...VIEW, available: true } });
    for (const [status, detail] of [[403, "not_a_member"], [403, "tenant_locked"], [404, "tenant_not_found"], [409, "no_agency_selected"], [400, "missing_tenant"]] as const) {
      expect(viewAnswer({ ok: false, status, detail })).toEqual({ status: 200, body: NO_CRM_CALENDAR });
    }
    expect(viewAnswer({ ok: false, status: 401, detail: "session_expired" }).status).toBe(401);
    expect(viewAnswer({ ok: false, status: 503, detail: "mail_not_configured" })).toEqual({ status: 503, body: { detail: "calendar_unavailable" } });
    expect(viewAnswer({ ok: false, status: 0, detail: "api_unreachable" }).status).toBe(503);
    expect(viewAnswer({ ok: false, status: 500, detail: "error" }).status).toBe(503);
    expect(NO_CRM_CALENDAR).toMatchObject({ available: false, connections: [], providers: { google: false, microsoft: false } });
  });

  it("a write passes the CRM's answer through", () => {
    expect(writeAnswer({ ok: true, status: 201, data: { calendar_connection_id: CONNECTION } }).status).toBe(201);
    expect(writeAnswer({ ok: false, status: 409, detail: "calendar_disconnect_pending" })).toEqual({ status: 409, body: { detail: "calendar_disconnect_pending" } });
    expect(writeAnswer({ ok: false, status: 0, detail: "api_unreachable" }).status).toBe(503);
  });
});

describe("GET /api/studio/calendar (the Integrations page load)", () => {
  it("a studio session with no CRM agency picked is served: the studio's tenant goes to the API", async () => {
    const upstream = vi.fn(async () => Response.json(VIEW));
    vi.stubGlobal("fetch", upstream);
    const res = await GET(request("/api/studio/calendar"), params());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ...VIEW, available: true });
    expect(res.headers.get("cache-control")).toBe("no-store");
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${ENV.CRM_API_ORIGIN}/crm/v1/calendar/connections`);
    expect((init.headers as Headers).get("x-aa-tenant-id")).toBe(STUDIO_TENANT);
  });

  it("no CRM role in the studio's agency: 200 'not available', never a 4xx the browser logs", async () => {
    for (const [status, detail] of [[403, "not_a_member"], [403, "tenant_locked"], [404, "tenant_not_found"]] as const) {
      vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail }, { status })));
      const res = await GET(request("/api/studio/calendar"), params());
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual(NO_CRM_CALENDAR);
    }
  });

  it("an expired partner token: 401 without calling the API; no studio session: the studio's own 401", async () => {
    const upstream = vi.fn(async () => Response.json(VIEW));
    vi.stubGlobal("fetch", upstream);
    expect((await GET(request("/api/studio/calendar", { cookie: signedIn(-60) }), params())).status).toBe(401);
    studio.ok = false;
    expect((await GET(request("/api/studio/calendar"), params())).status).toBe(401);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("an API outage stays visible (503)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "boom" }, { status: 500 })));
    expect((await GET(request("/api/studio/calendar"), params())).status).toBe(503);
  });

  it("the old browser path is gone: /bff/calendar/connections is 404, not the 409 the smoke saw", async () => {
    const upstream = vi.fn(async () => Response.json(VIEW));
    vi.stubGlobal("fetch", upstream);
    const res = await bffGet(request("/bff/calendar/connections"), { params: Promise.resolve({ path: ["calendar", "connections"] }) });
    expect(res.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe("POST /api/studio/calendar/*", () => {
  it("starts a connection with the studio's tenant and passes the CRM's answer through", async () => {
    const upstream = vi.fn(async () => Response.json({ authorize_url: "https://accounts.example.test/o" }));
    vi.stubGlobal("fetch", upstream);
    const res = await POST(request("/api/studio/calendar/oauth/start", { method: "POST", body: { provider: "google", scope: "mine" } }), params("oauth", "start"));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ authorize_url: "https://accounts.example.test/o" });
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${ENV.CRM_API_ORIGIN}/crm/v1/calendar/connections/oauth/start`);
    expect(JSON.parse(String(init.body))).toEqual({ provider: "google", scope: "mine" });
    expect((init.headers as Headers).get("x-aa-tenant-id")).toBe(STUDIO_TENANT);
  });

  it("keeps the CRM's refusal code for the card's wording", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "oauth_state_expired" }, { status: 409 })));
    const res = await POST(request("/api/studio/calendar/oauth/complete", { method: "POST", body: { code: "c", state: "cal.s" } }), params("oauth", "complete"));
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ detail: "oauth_state_expired" });
  });

  it("disconnects without a body; refuses a bad body and a cross-site write before the API", async () => {
    const upstream = vi.fn(async () => Response.json({ status: "revoked" }));
    vi.stubGlobal("fetch", upstream);
    const done = await POST(request(`/api/studio/calendar/${CONNECTION}/disconnect`, { method: "POST" }), params(CONNECTION, "disconnect"));
    expect(done.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(1);
    const bad = await POST(request("/api/studio/calendar/oauth/start", { method: "POST", body: "[1]" }), params("oauth", "start"));
    expect(bad.status).toBe(400);
    const foreign = await POST(
      request("/api/studio/calendar/oauth/start", { method: "POST", body: { provider: "google" }, origin: "https://evil.example.test" }),
      params("oauth", "start"),
    );
    expect(foreign.status).toBe(403);
    expect(upstream).toHaveBeenCalledTimes(1);
  });
});

describe("guard: the studio never calls the CRM's browser BFF", () => {
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const p = path.join(dir, name);
      return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(name) ? [p] : [];
    });
  }

  it("no file under app/(studio) imports bffClient or names a /bff/ path (a studio session picks no CRM agency)", () => {
    const root = path.join(__dirname, "..", "app", "(studio)");
    const offenders = files(root).filter((f) => {
      const src = readFileSync(f, "utf8");
      return src.includes("@/lib/bffClient") || /["'`]\/bff\//.test(src);
    });
    expect(offenders.map((f) => path.relative(root, f))).toEqual([]);
  });
});
