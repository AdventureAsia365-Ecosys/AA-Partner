// AA-1019 — the AA staff console: configuration, the staff session kept apart from the agency session (names, paths,
// key, client), the staff sign-in / refresh / sign-out routes, the staff BFF allow-list onto /crm/staff/v1 with its
// group check, the proxy gate for /staff, and the page helpers. Fake values only; fetch is stubbed.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as bffGet, POST as bffPost } from "@/app/bff/[...path]/route";
import { GET as healthz } from "@/app/healthz/route";
import { GET as staffBffGet, POST as staffBffPost } from "@/app/staff/bff/[...path]/route";
import { GET as staffCallback } from "@/app/staff/auth/callback/route";
import { GET as staffRefresh } from "@/app/staff/auth/refresh/route";
import { GET as staffSignIn } from "@/app/staff/auth/sign-in/route";
import { POST as staffSignOut } from "@/app/staff/auth/sign-out/route";
import { cookieNames, readSession, refreshCookie, sessionCookie } from "@/lib/session";
import { open } from "@/lib/seal";
import { allowedQuery, groupAllowed, matchStaffBff, STAFF_BFF_ROUTES } from "@/lib/staffBff";
import {
  boardDays,
  flightText,
  homeFor,
  manifestFilename,
  manifestQuery,
  minutesValid,
  missingItems,
  pickFields,
  purgeLabel,
  purgeTone,
  refusalKey,
  ticketValid,
  uuidParam,
} from "@/lib/staffConsole";
import {
  clearedStaffCookies,
  readStaffSession,
  safeStaffNext,
  staffCookieNames,
  staffRefreshCookie,
  staffSessionCookie,
  staffTxCookie,
} from "@/lib/staffSession";
import { contentSecurityPolicy, gate } from "@/proxy";
import { cookieHeader, ENV, fakeAccessToken, fakeStaffToken, STAFF_ISS, testConfig } from "./helpers";

const ORIGIN = "https://partner.example.test";
const TENANT = "11111111-2222-4333-8444-555555555555";
const TRIP = "66666666-7777-4888-8999-aaaaaaaaaaaa";
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

const SAME_ORIGIN = { origin: ORIGIN, "sec-fetch-site": "same-origin" };

function staffSignedIn(groups: ("crm-ops" | "crm-support")[] = ["crm-ops"]): string {
  const now = Math.floor(Date.now() / 1000);
  const s = staffSessionCookie(cfg, { at: "staff-token", atExp: now + 600, iss: STAFF_ISS, sub: "staff-1", cid: ENV.STAFF_COGNITO_CLIENT_ID, groups });
  const r = staffRefreshCookie(cfg, "staff-rt", "staff-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

function agencySignedIn(): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + 600, iss: "i", sub: "sub-1", cid: "c", tid: TENANT });
  const r = refreshCookie(cfg, "rt-1", "sub-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

function params(...path: string[]) {
  return { params: Promise.resolve({ path }) };
}

describe("configuration", () => {
  it("reads the staff client and key, and turns the console off without them", () => {
    expect(cfg.staffAuthConfigured).toBe(true);
    expect(cfg.staffCognitoClientId).toBe("staffclient456");
    const off = testConfig({ STAFF_SESSION_SECRET: "short" });
    expect(off.staffAuthConfigured).toBe(false);
    expect(off.staffProblems).toEqual(["STAFF_SESSION_SECRET"]);
    expect(off.authConfigured).toBe(true); // the agency app is unaffected
  });

  it("refuses the agency's client id or session key for the staff console", () => {
    expect(testConfig({ STAFF_COGNITO_CLIENT_ID: ENV.COGNITO_CLIENT_ID }).staffProblems).toContain("STAFF_COGNITO_CLIENT_ID");
    expect(testConfig({ STAFF_SESSION_SECRET: ENV.WEB_SESSION_SECRET }).staffProblems).toContain("STAFF_SESSION_SECRET");
    expect(testConfig({ STAFF_COGNITO_DOMAIN: "http://staff-auth.example.test" }).staffProblems).toContain("STAFF_COGNITO_DOMAIN");
  });

  it("reports the staff console on /healthz (names only)", async () => {
    const body = await healthz().json();
    expect(body).toMatchObject({ auth_configured: true, staff_auth_configured: true });
    expect(JSON.stringify(body)).not.toContain("STAFF_");
  });
});

describe("the staff session is kept apart from the agency session", () => {
  it("uses its own names and paths, never Path=/", () => {
    const n = staffCookieNames(cfg);
    expect(n).toEqual({ session: "__Secure-aa_crm_staff_s", refresh: "__Secure-aa_crm_staff_r", tx: "__Secure-aa_crm_staff_tx" });
    expect(Object.values(n)).not.toContain(cookieNames(cfg).session);
    const s = staffSessionCookie(cfg, { at: "a", atExp: 1, iss: "i", sub: "s", cid: "staffclient456", groups: ["crm-ops"] });
    expect([s.path, s.httpOnly, s.secure, s.sameSite]).toEqual(["/staff", true, true, "lax"]);
    expect(staffRefreshCookie(cfg, "rt", "s").path).toBe("/staff/auth");
    expect(staffTxCookie(cfg, { state: "x", verifier: "v", nonce: "n", next: "/staff" }).path).toBe("/staff/auth");
    expect(clearedStaffCookies(cfg).map((c) => [c.name, c.path, c.maxAge])).toEqual([
      ["__Secure-aa_crm_staff_s", "/staff", 0],
      ["__Secure-aa_crm_staff_r", "/staff/auth", 0],
      ["__Secure-aa_crm_staff_tx", "/staff/auth", 0],
    ]);
  });

  it("seals with the staff key: the agency key cannot open it and an agency cookie does not read as staff", () => {
    const s = staffSessionCookie(cfg, { at: "a", atExp: 9e9, iss: "i", sub: "s", cid: "staffclient456", groups: ["crm-ops"] });
    expect(open(cfg.sessionSecret, "session", s.value)).toBeNull();
    expect(open(cfg.sessionSecret, "staff-session", s.value)).toBeNull();
    expect(open(cfg.staffSessionSecret, "staff-session", s.value)).not.toBeNull();
    // An agency session value placed under the staff cookie name is no staff session (another key and purpose).
    const agency = sessionCookie(cfg, { at: "a", atExp: 9e9, iss: "i", sub: "s", cid: "staffclient456", tid: null });
    expect(readStaffSession(cfg, { get: (n) => (n === staffCookieNames(cfg).session ? { value: agency.value } : undefined) })).toBeNull();
    // And the staff value under the agency name is no agency session.
    expect(readSession(cfg, { get: (n) => (n === cookieNames(cfg).session ? { value: s.value } : undefined) })).toBeNull();
  });

  it("refuses a staff session sealed for another client id", () => {
    const s = staffSessionCookie(cfg, { at: "a", atExp: 9e9, iss: "i", sub: "s", cid: "otherclient", groups: ["crm-ops"] });
    expect(readStaffSession(cfg, { get: () => ({ value: s.value }) })).toBeNull();
  });

  it("returns only to console pages after sign-in", () => {
    expect(safeStaffNext("/staff/ops?days=30")).toBe("/staff/ops?days=30");
    expect(safeStaffNext("/staff")).toBe("/staff");
    for (const bad of ["/", "/pipeline", "//evil.example.test", "/staff/auth/sign-out", "/staff/bff/ops-releases", "/staffing", null, "https://x"]) {
      expect(safeStaffNext(bad as string | null)).toBe("/staff");
    }
  });
});

describe("staff sign-in, callback, refresh and sign-out", () => {
  it("signs in with the WORKFORCE crm-staff client and its own callback", () => {
    const res = staffSignIn(request("/staff/auth/sign-in?next=/staff/support"));
    const url = new URL(res.headers.get("location")!);
    expect(url.origin).toBe("https://staff-auth.example.test");
    expect(url.searchParams.get("client_id")).toBe("staffclient456");
    expect(url.searchParams.get("redirect_uri")).toBe(`${ORIGIN}/staff/auth/callback`);
    const set = res.headers.getSetCookie();
    expect(set).toHaveLength(1);
    expect(set[0]).toMatch(/^__Secure-aa_crm_staff_tx=.*Path=\/staff\/auth/);
  });

  it("round-trips into staff cookies only, with the token's staff groups", async () => {
    const start = staffSignIn(request("/staff/auth/sign-in?next=/staff/ops"));
    const state = new URL(start.headers.get("location")!).searchParams.get("state")!;
    const token = fakeStaffToken(["crm-ops", "cis-admin"]);
    const fetchMock = vi.fn(async () => Response.json({ access_token: token, refresh_token: "srt", expires_in: 900 }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await staffCallback(request(`/staff/auth/callback?code=c1&state=${state}`, { cookie: cookieHeader(start) }));
    expect(res.headers.get("location")).toBe(`${ORIGIN}/staff/ops`);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://staff-auth.example.test/oauth2/token");
    expect((init.headers as Record<string, string>).Authorization).toBe(`Basic ${Buffer.from("staffclient456:dddddddd").toString("base64")}`);
    const names = res.headers.getSetCookie().map((c) => c.split("=")[0]);
    expect(names.every((n) => n.includes("aa_crm_staff_"))).toBe(true);
    const jar = cookieHeader(res);
    const session = readStaffSession(cfg, { get: (n) => (jar.includes(`${n}=`) ? { value: jar.split(`${n}=`)[1].split(";")[0] } : undefined) });
    expect(session?.groups).toEqual(["crm-ops"]);
    expect(session?.cid).toBe("staffclient456");
  });

  it("refuses a token of the agency client and a staff user without a staff group", async () => {
    const start = staffSignIn(request("/staff/auth/sign-in"));
    const state = new URL(start.headers.get("location")!).searchParams.get("state")!;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: fakeAccessToken(), refresh_token: "x", expires_in: 900 })));
    const wrong = await staffCallback(request(`/staff/auth/callback?code=c&state=${state}`, { cookie: cookieHeader(start) }));
    expect(wrong.headers.get("location")).toBe(`${ORIGIN}/staff/signed-out?reason=failed`);

    const again = staffSignIn(request("/staff/auth/sign-in"));
    const state2 = new URL(again.headers.get("location")!).searchParams.get("state")!;
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (u: string) => {
      calls.push(u);
      return u.endsWith("/oauth2/token")
        ? Response.json({ access_token: fakeStaffToken(["cis-admin"]), refresh_token: "r", expires_in: 900 })
        : new Response("", { status: 200 });
    }));
    const noGroup = await staffCallback(request(`/staff/auth/callback?code=c&state=${state2}`, { cookie: cookieHeader(again) }));
    expect(noGroup.headers.get("location")).toBe(`${ORIGIN}/staff/signed-out?reason=no_group`);
    expect(calls).toEqual(["https://staff-auth.example.test/oauth2/token", "https://staff-auth.example.test/oauth2/revoke"]);
    expect(cookieHeader(noGroup)).toBe("");
  });

  it("refreshes with the staff refresh token and keeps the agency cookies out of it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ access_token: fakeStaffToken(["crm-support"]), expires_in: 900 })));
    const res = await staffRefresh(request("/staff/auth/refresh?next=/staff/support", { cookie: staffSignedIn() }));
    expect(res.headers.get("location")).toBe(`${ORIGIN}/staff/support`);
    expect(res.headers.getSetCookie().map((c) => c.split("=")[0]).sort()).toEqual(["__Secure-aa_crm_staff_r", "__Secure-aa_crm_staff_s"]);
    // No staff refresh token (an agency session only): sign in again, nothing renewed.
    const none = await staffRefresh(request("/staff/auth/refresh?next=/staff/ops", { cookie: agencySignedIn() }));
    expect(none.headers.get("location")).toBe(`${ORIGIN}/staff/auth/sign-in?next=%2Fstaff%2Fops`);
  });

  it("signs out of the staff console only, same-origin only", async () => {
    const revoke = vi.fn(async () => new Response("", { status: 200 }));
    vi.stubGlobal("fetch", revoke);
    const res = await staffSignOut(request("/staff/auth/sign-out", { method: "POST", headers: SAME_ORIGIN, cookie: `${staffSignedIn()}; ${agencySignedIn()}` }));
    expect(res.status).toBe(303);
    const logout = new URL(res.headers.get("location")!);
    expect(`${logout.origin}${logout.pathname}`).toBe("https://staff-auth.example.test/logout");
    expect(logout.searchParams.get("client_id")).toBe("staffclient456");
    expect(logout.searchParams.get("logout_uri")).toBe(`${ORIGIN}/staff/signed-out`);
    const cleared = res.headers.getSetCookie().map((c) => c.split("=")[0]).sort();
    expect(cleared).toEqual(["__Secure-aa_crm_staff_r", "__Secure-aa_crm_staff_s", "__Secure-aa_crm_staff_tx"]);
    expect(revoke).toHaveBeenCalledTimes(1);
    const evil = await staffSignOut(request("/staff/auth/sign-out", { method: "POST", headers: { origin: "https://evil.example.test" }, cookie: staffSignedIn() }));
    expect(evil.status).toBe(403);
  });
});

describe("the staff BFF", () => {
  it("lists /crm/staff/v1 paths only, each with its groups", () => {
    expect(matchStaffBff("GET", "ops-releases")?.groups).toEqual(["crm-ops"]);
    expect(matchStaffBff("GET", "support/agencies")?.groups).toEqual(["crm-support"]);
    expect(matchStaffBff("POST", "breakglass/sessions")?.groups).toEqual(["crm-ops"]);
    expect(matchStaffBff("GET", `agencies/${TENANT}/trips/${TRIP}/ops-release/manifest`)).not.toBeNull();
    for (const [m, p] of [["GET", "pipeline"], ["GET", "me"], ["POST", "ops-releases"], ["GET", `agencies/${TENANT}/export`], ["GET", "tenants"]]) {
      expect(matchStaffBff(m, p)).toBeNull();
    }
    expect(STAFF_BFF_ROUTES.every((r) => r.method === "GET" || /sessions(\\\/end)?\$/.test(r.pattern.source))).toBe(true);
    expect(groupAllowed(matchStaffBff("GET", "ops-releases")!, ["crm-support"])).toBe(false);
  });

  it("forwards only the listed query parameters", () => {
    const q = allowedQuery("ops-releases", new URLSearchParams("days=30&tenant=x&days=abc"));
    expect(q.toString()).toBe("days=30");
    const m = allowedQuery(`agencies/${TENANT}/trips/${TRIP}/ops-release/manifest`, new URLSearchParams("fields=name&fields=passport!&x=1"));
    expect(m.getAll("fields")).toEqual(["name"]);
    const t = allowedQuery(`agencies/${TENANT}/trips`, new URLSearchParams({ cursor: "2026-04-03T10:00:00+00:00|abc", limit: "25", x: "1" }));
    expect(t.get("cursor")).toBe("2026-04-03T10:00:00+00:00|abc");
    expect(t.get("x")).toBeNull();
  });

  it("forwards with the staff token, never the agency session, and checks the group first", async () => {
    const fetchMock = vi.fn(async () => Response.json({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const ok = await staffBffGet(request("/staff/bff/ops-releases?days=30&tenant=x", { cookie: staffSignedIn(["crm-ops"]) }), params("ops-releases"));
    expect(ok.status).toBe(200);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${ORIGIN}/crm/staff/v1/ops-releases?days=30`);
    const h = init.headers as Headers;
    expect(h.get("authorization")).toBe("Bearer staff-token");
    expect(h.get("x-aa-tenant-id")).toBeNull();

    fetchMock.mockClear();
    const noGroup = await staffBffGet(request("/staff/bff/ops-releases", { cookie: staffSignedIn(["crm-support"]) }), params("ops-releases"));
    expect([noGroup.status, (await noGroup.json()).detail]).toEqual([403, "missing_group"]);
    const agencyOnly = await staffBffGet(request("/staff/bff/ops-releases", { cookie: agencySignedIn() }), params("ops-releases"));
    expect([agencyOnly.status, (await agencyOnly.json()).detail]).toEqual([401, "session_expired"]);
    const unlisted = await staffBffGet(request("/staff/bff/pipeline", { cookie: staffSignedIn() }), params("pipeline"));
    expect(unlisted.status).toBe(404);
    const escape = await staffBffGet(request("/staff/bff/x", { cookie: staffSignedIn() }), params("..", "v1", "pipeline"));
    expect(escape.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a cross-site session start before reading anything", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const body = JSON.stringify({ tenant_id: TENANT, ticket_ref: "INC-7", reason_code: "service_outage", minutes: 15 });
    const res = await staffBffPost(
      request("/staff/bff/breakglass/sessions", { method: "POST", headers: { origin: "https://evil.example.test" }, cookie: staffSignedIn(), body }),
      params("breakglass", "sessions"),
    );
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ kind: "breakglass" }, { status: 201 })));
    const ok = await staffBffPost(
      request("/staff/bff/breakglass/sessions", { method: "POST", headers: { ...SAME_ORIGIN, "content-type": "application/json" }, cookie: staffSignedIn(), body }),
      params("breakglass", "sessions"),
    );
    expect(ok.status).toBe(201);
  });

  it("the agency BFF never accepts a staff session", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const res = await bffGet(request("/bff/me", { cookie: staffSignedIn() }), params("me"));
    expect([res.status, (await res.json()).detail]).toEqual([401, "session_expired"]);
    const post = await bffPost(request("/bff/support-access/revoke", { method: "POST", headers: SAME_ORIGIN, cookie: staffSignedIn() }), params("support-access", "revoke"));
    expect(post.status).toBe(401);
  });
});

describe("the proxy gate for /staff", () => {
  const go = (path: string, cookie?: string) => gate(request(path, { cookie }));

  it("sends a console page without a staff session to the staff refresh, even with an agency session", () => {
    for (const cookie of [undefined, agencySignedIn()]) {
      const res = go("/staff/ops", cookie);
      expect(res.status).toBe(307);
      expect(res.headers.get("location")).toBe(`${ORIGIN}/staff/auth/refresh?next=%2Fstaff%2Fops`);
    }
  });

  it("passes a valid staff session, the sign-in flow and the staff BFF; an agency page still needs the agency session", () => {
    expect(go("/staff/support", staffSignedIn()).headers.get("x-middleware-next")).toBe("1");
    expect(go("/staff/auth/sign-in").headers.get("x-middleware-next")).toBe("1");
    expect(go("/staff/signed-out").headers.get("x-middleware-next")).toBe("1");
    expect(go("/staff/bff/ops-releases").headers.get("x-middleware-next")).toBe("1");
    const agencyPage = go("/pipeline", staffSignedIn());
    expect(agencyPage.headers.get("location")).toBe(`${ORIGIN}/auth/refresh?next=%2Fpipeline`);
  });

  it("allows the WORKFORCE logout as a form target on staff pages", () => {
    const csp = go("/staff/ops", staffSignedIn()).headers.get("content-security-policy")!;
    expect(csp).toContain("form-action 'self' https://auth.example.test https://staff-auth.example.test");
    expect(contentSecurityPolicy("https://auth.example.test")).not.toContain("staff-auth");
  });

  it("serves no staff page on the client host and says so when the console is off", () => {
    const client = gate(new NextRequest("https://go.example.test/staff/ops", { headers: { host: "go.example.test" } }));
    expect(client.status).toBe(404);
    cfg = testConfig({ STAFF_COGNITO_CLIENT_SECRET: "" });
    expect(go("/staff/ops").headers.get("location")).toBe(`${ORIGIN}/staff/signed-out?reason=unavailable`);
  });
});

describe("page helpers", () => {
  it("words the board", () => {
    expect(boardDays("30")).toBe(30);
    expect(boardDays("7")).toBe(60);
    expect(boardDays(undefined)).toBe(60);
    expect(purgeLabel(12)).toEqual({ key: "staff.ops.purgeIn", n: 12 });
    expect(purgeLabel(0).key).toBe("staff.ops.purgeToday");
    expect(purgeLabel(null).key).toBe("staff.ops.purgeUnknown");
    expect([purgeTone(3), purgeTone(30), purgeTone(null)]).toEqual(["warning", "neutral", "neutral"]);
    expect(missingItems({ travellers: 2, complete: 0, missing: { arrival_flight: 2, stage3: 1, waiver: 0 } })).toEqual([
      { item: "stage3", n: 1 },
      { item: "arrival_flight", n: 2 },
    ]);
  });

  it("builds the manifest request and a safe file name", () => {
    expect(pickFields(["mobility", "name", "passport"])).toEqual(["name", "mobility"]);
    expect(manifestQuery(["name", "age_band"]).toString()).toBe("fields=name&fields=age_band");
    expect(manifestFilename({ filename: "manifest-1a2b3c4d-AAOPS-0A1B2C3D4E.csv", watermark: "AAOPS-0A1B2C3D4E" })).toBe(
      "manifest-1a2b3c4d-AAOPS-0A1B2C3D4E.csv",
    );
    expect(manifestFilename({ filename: "../../etc/x", watermark: "AAOPS-1" })).toBe("manifest-AAOPS-1.csv");
    expect(flightText({ flight_no: "QX123", airport: "HAN", sched_local: "2026-04-03T10:20", tz: "Asia/Bangkok" })).toBe(
      "QX123 HAN 2026-04-03 10:20 (Asia/Bangkok)",
    );
    expect(flightText(null)).toBe("");
  });

  it("checks the S-04 inputs like the API", () => {
    expect(["SUP-1042", "INC-7", " sup-1 "].map(ticketValid)).toEqual([true, true, true]);
    expect(["free text", "SUP-", "-12", "1SUP-2"].map(ticketValid)).toEqual([false, false, false, false]);
    expect([15, 60, 30].map(minutesValid)).toEqual([true, true, true]);
    expect([14, 61, 20.5].map(minutesValid)).toEqual([false, false, false]);
    expect(refusalKey("support_session_ended")).toBe("staff.refused.support_session_ended");
    expect(refusalKey("e2e_synthetic_only")).toBe("staff.refused.e2e_synthetic_only");
    expect(refusalKey("boom")).toBe("staff.error");
    expect(homeFor(["crm-support"])).toBe("/staff/support");
    expect(homeFor(["crm-support", "crm-ops"])).toBe("/staff/ops");
    expect(uuidParam(TENANT.toUpperCase())).toBe(TENANT);
    expect(uuidParam("../x")).toBeNull();
  });
});
