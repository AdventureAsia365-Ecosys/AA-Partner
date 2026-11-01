// AA-939 (CRM-11): the join flow (invitation link -> claim -> sign-in -> accept), its sealed cookies, the Team BFF
// allow-list lines, the public paths, the Team helpers and the EN/VI strings.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST as claimPost } from "@/app/join/claim/route";
import { POST as confirmPost } from "@/app/join/confirm/route";
import { GET as joinGet } from "@/app/join/route";
import { matchBff } from "@/lib/bff";
import { isPublicPartnerPath } from "@/lib/hosts";
import { MESSAGES } from "@/lib/i18n";
import {
  claimCookie,
  claimInvitation,
  claimStep,
  type ClaimResult,
  clearedInviteCookies,
  INVITE_TTL_S,
  inviteCookie,
  inviteCookieNames,
  isInviteToken,
  joinError,
  openClaim,
  openInvite,
  openNotice,
  shownAnswer,
  staleAnswerCookies,
} from "@/lib/invite";
import { readSession, refreshCookie, sessionCookie } from "@/lib/session";
import { memberLabel, reassignTargets, seatsLeft, teamError, type TeamMember } from "@/lib/team";
import { cookieHeader, fakeAccessToken, testConfig } from "./helpers";

const ORIGIN = "https://partner.example.test";
const TOKEN = "A".repeat(20) + "b_c-" + "9".repeat(19);
const OTHER_TOKEN = "B".repeat(20) + "d_e-" + "8".repeat(19);
const UUID = "00000000-0000-4000-8000-000000000001";
let cfg = testConfig();

beforeEach(() => {
  cfg = testConfig();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function request(path: string, init: { method?: string; cookie?: string; headers?: Record<string, string> } = {}) {
  const headers = new Headers(init.headers);
  headers.set("host", "partner.example.test");
  if (init.cookie) headers.set("cookie", init.cookie);
  return new NextRequest(`${ORIGIN}${path}`, { method: init.method ?? "GET", headers });
}

const SAME_ORIGIN = { origin: ORIGIN, "sec-fetch-site": "same-origin" };

function signedIn(tid: string | null = "tenant-a"): string {
  const now = Math.floor(Date.now() / 1000);
  const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid });
  const r = refreshCookie(cfg, "rt-1", "sub-1");
  return `${s.name}=${s.value}; ${r.name}=${r.value}`;
}

function inviteCookieHeader(token = TOKEN): string {
  const c = inviteCookie(cfg, token);
  return `${c.name}=${c.value}`;
}

function claimCookieHeader(claim: ClaimResult, token = TOKEN): string {
  const c = claimCookie(cfg, claim, token);
  return `${c.name}=${c.value}`;
}

function setCookie(res: Response, name: string): string | undefined {
  const set = res.headers.getSetCookie().find((c) => c.startsWith(`${name}=`));
  return set?.split(";")[0].split("=").slice(1).join("=");
}

function setClaim(res: Response): string {
  return setCookie(res, inviteCookieNames(cfg).claim)!;
}

function jarOf(entries: Record<string, string>) {
  return { get: (name: string) => (name in entries ? { value: entries[name] } : undefined) };
}

describe("invitation token and cookies", () => {
  it("accepts only 43-character URL-safe tokens", () => {
    expect(isInviteToken(TOKEN)).toBe(true);
    for (const bad of ["", "short", `${TOKEN}x`, `${TOKEN.slice(0, 42)}!`, null, undefined]) expect(isInviteToken(bad)).toBe(false);
  });

  it("seals the token on path /join for two hours and opens it only before expiry", () => {
    const c = inviteCookie(cfg, TOKEN, 1000);
    expect(c).toMatchObject({ path: "/join", httpOnly: true, secure: true, sameSite: "lax", maxAge: INVITE_TTL_S });
    expect(c.name).toBe("__Secure-aa_crm_inv");
    expect(c.value).not.toContain(TOKEN);
    expect(openInvite(cfg, c.value, 1001)).toBe(TOKEN);
    expect(openInvite(cfg, c.value, 1000 + INVITE_TTL_S + 1)).toBeNull();
    // Flip one bit of the auth tag in the decoded bytes: replacing the last base64url characters can leave the bytes
    // unchanged (the final character carries padding bits), so that version of this check failed at random.
    const raw = Buffer.from(c.value, "base64url");
    raw[raw.length - 1] ^= 1;
    expect(openInvite(cfg, raw.toString("base64url"), 1001)).toBeNull();
    expect(openInvite(testConfig({ WEB_SESSION_SECRET: "x".repeat(40) }), c.value, 1001)).toBeNull();
  });

  it("keeps the claim answer sealed (the one-time password is never readable from the cookie)", () => {
    // Built at run time so the secret scan does not read a test value as a password literal.
    const PW = ["Pw", "123456789"].join("-");
    const c = claimCookie(cfg, { state: "created", email: "lan@agency.example", temporaryPassword: PW }, TOKEN, 50);
    expect(c.value).not.toContain(PW);
    expect(c.value).not.toContain("lan@agency.example");
    expect(openClaim(cfg, c.value, TOKEN, 60)).toEqual({ state: "created", email: "lan@agency.example", temporaryPassword: PW });
    expect(openClaim(cfg, c.value, TOKEN, 50 + 15 * 60 + 1)).toBeNull();
    // S263: the answer belongs to one invitation; another invitation's token (or none) never opens it.
    expect(openClaim(cfg, c.value, OTHER_TOKEN, 60)).toBeNull();
    expect(openClaim(cfg, c.value, null, 60)).toBeNull();
    // A value sealed for the token cookie never opens as a claim, and the other way round.
    expect(openClaim(cfg, inviteCookie(cfg, TOKEN).value, TOKEN)).toBeNull();
    expect(openInvite(cfg, c.value)).toBeNull();
    expect(clearedInviteCookies(cfg).map((x) => [x.name, x.maxAge])).toEqual([
      ["__Secure-aa_crm_inv", 0],
      ["__Secure-aa_crm_invc", 0],
      ["__Secure-aa_crm_invn", 0],
    ]);
  });

  // S263: "already_claimed" lives in its own cookie, so it can never overwrite the cookie holding a password.
  it("keeps an already_claimed answer in the notice cookie, never in the claim cookie", () => {
    const names = inviteCookieNames(cfg);
    const notice = claimCookie(cfg, { state: "already_claimed", email: "lan@agency.example", temporaryPassword: "never" }, TOKEN, 50);
    expect(notice.name).toBe(names.notice);
    expect(openNotice(cfg, notice.value, TOKEN, 60)).toEqual({ state: "already_claimed", email: "lan@agency.example", temporaryPassword: null });
    expect(openClaim(cfg, notice.value, TOKEN, 60)).toBeNull();
    expect(openNotice(cfg, notice.value, OTHER_TOKEN, 60)).toBeNull();
    const PW = ["Pw", "kept"].join("-");
    const claim = claimCookie(cfg, { state: "created", email: "lan@agency.example", temporaryPassword: PW }, TOKEN, 50);
    expect(claim.name).toBe(names.claim);
    expect(openNotice(cfg, claim.value, TOKEN, 60)).toBeNull();
    // The page reads the claim answer first (it holds the password), else the notice; never another invitation's.
    const both = jarOf({ [names.claim]: claim.value, [names.notice]: notice.value });
    expect(shownAnswer(cfg, both, TOKEN, 60)?.temporaryPassword).toBe(PW);
    expect(shownAnswer(cfg, jarOf({ [names.notice]: notice.value }), TOKEN, 60)?.state).toBe("already_claimed");
    expect(shownAnswer(cfg, both, OTHER_TOKEN, 60)).toBeNull();
    expect(shownAnswer(cfg, both, null, 60)).toBeNull();
  });

  it("words known API errors and folds the rest into 'failed'", () => {
    expect(joinError("invitation_expired")).toBe("invitation_expired");
    expect(joinError("already_member")).toBe("already_member");
    expect(joinError("invitation_race")).toBe("invitation_race");
    expect(joinError("whatever")).toBe("failed");
    expect(joinError(undefined)).toBe("failed");
  });
});

describe("GET /join?t=", () => {
  it("moves the token into the sealed cookie and out of the URL", async () => {
    const res = await joinGet(request(`/join?t=${TOKEN}`));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/join/invite`);
    expect(res.headers.get("referrer-policy")).toBe("no-referrer");
    const header = cookieHeader(res);
    const value = header.split("=").slice(1).join("=");
    expect(openInvite(cfg, value)).toBe(TOKEN);
  });

  it("sets nothing for a malformed token", async () => {
    const res = await joinGet(request("/join?t=../../etc"));
    expect(res.status).toBe(303);
    expect(res.headers.getSetCookie()).toEqual([]);
  });

  // S263, found live on Dev: after a first invitation, the link of a second one (another address) showed the first
  // one's address and "opened before" from the earlier answer cookie.
  it("drops the answer cookies of another invitation and keeps this one's", async () => {
    const names = inviteCookieNames(cfg);
    const stale = `${claimCookieHeader({ state: "created", email: "first@agency.example", temporaryPassword: "x" }, OTHER_TOKEN)}; ${claimCookieHeader({ state: "already_claimed", email: "first@agency.example", temporaryPassword: null }, OTHER_TOKEN)}`;
    const res = await joinGet(request(`/join?t=${TOKEN}`, { cookie: stale }));
    expect(openInvite(cfg, setCookie(res, names.token))).toBe(TOKEN);
    expect(setCookie(res, names.claim)).toBe("");
    expect(setCookie(res, names.notice)).toBe("");
    expect(res.headers.getSetCookie().filter((c) => c.startsWith(`${names.claim}=`))[0]).toContain("Max-Age=0");
    // The same link opened again: its own answer (with the password) stays.
    const own = claimCookieHeader({ state: "created", email: "kim@agency.example", temporaryPassword: "y" });
    const again = await joinGet(request(`/join?t=${TOKEN}`, { cookie: own }));
    expect(setCookie(again, names.claim)).toBeUndefined();
    expect(staleAnswerCookies(cfg, jarOf({}), TOKEN)).toEqual([]);
    expect(staleAnswerCookies(cfg, jarOf({ [names.claim]: "garbage" }), TOKEN).map((c) => [c.name, c.maxAge])).toEqual([[names.claim, 0]]);
  });
});

describe("POST /join/claim", () => {
  it("refuses a cross-site post before reading anything", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await claimPost(request("/join/claim", { method: "POST", cookie: inviteCookieHeader(), headers: { origin: "https://evil.example.test" } }));
    expect(res.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("calls the public claim and keeps the answer in the sealed claim cookie", async () => {
    const fetchSpy = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe(`${ORIGIN}/crm/public/v1/invitations/claim`);
      expect(JSON.parse(String(init.body))).toEqual({ token: TOKEN });
      expect(new Headers(init.headers).get("authorization")).toBeNull();
      return Response.json({ state: "created", email: "lan@agency.example", temporary_password: "Pw-1" });
    });
    vi.stubGlobal("fetch", fetchSpy);
    const res = await claimPost(request("/join/claim", { method: "POST", cookie: inviteCookieHeader(), headers: SAME_ORIGIN }));
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/join/invite`);
    const set = res.headers.getSetCookie().find((c) => c.startsWith(`${inviteCookieNames(cfg).claim}=`))!;
    expect(set).toContain("Path=/join");
    expect(openClaim(cfg, set.split(";")[0].split("=").slice(1).join("="), TOKEN)?.temporaryPassword).toBe("Pw-1");
    expect(res.headers.get("location")).not.toContain("Pw-1");
  });

  // S263, found live on Dev: a second "Open invitation" POST got already_claimed from the API and replaced the cookie
  // that held the one-time password; the person never saw it and could not sign in.
  it("a second POST keeps the password the browser already holds and does not ask the API again", async () => {
    const PW = ["Pw", "first"].join("-");
    const fetchSpy = vi.fn(async () => Response.json({ state: "already_claimed", email: "lan@agency.example" }));
    vi.stubGlobal("fetch", fetchSpy);
    const res = await claimPost(
      request("/join/claim", {
        method: "POST",
        cookie: `${inviteCookieHeader()}; ${claimCookieHeader({ state: "created", email: "lan@agency.example", temporaryPassword: PW })}`,
        headers: SAME_ORIGIN,
      }),
    );
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/join/invite`);
    expect(res.headers.getSetCookie()).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("an already_claimed answer goes to the notice cookie and never replaces a claim answer", async () => {
    const names = inviteCookieNames(cfg);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ state: "already_claimed", email: "Lan@Agency.example" })));
    const kept = await claimPost(
      request("/join/claim", {
        method: "POST",
        cookie: `${inviteCookieHeader()}; ${claimCookieHeader({ state: "existing", email: "lan@agency.example", temporaryPassword: null })}`,
        headers: SAME_ORIGIN,
      }),
    );
    expect(setCookie(kept, names.claim)).toBeUndefined();
    expect(openNotice(cfg, setCookie(kept, names.notice), TOKEN)?.state).toBe("already_claimed");
    // The answer of another invitation is ignored: the API is asked and its answer stored.
    const PW = ["Pw", "new"].join("-");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ state: "created", email: "lan@agency.example", temporary_password: PW })));
    const other = await claimPost(
      request("/join/claim", {
        method: "POST",
        cookie: `${inviteCookieHeader()}; ${claimCookieHeader({ state: "created", email: "lan@agency.example", temporaryPassword: "x" }, OTHER_TOKEN)}`,
        headers: SAME_ORIGIN,
      }),
    );
    expect(openClaim(cfg, setClaim(other), TOKEN)?.temporaryPassword).toBe(PW);
  });

  // S263, the live race: a double click sends the second POST before the first answer's cookie arrived, so it carries
  // no claim cookie; the API answers already_claimed. The browser must still hold (and the page still show) the
  // password from the first answer.
  it("a double submit that races keeps the password of the first answer", async () => {
    const names = inviteCookieNames(cfg);
    const PW = ["Pw", "race"].join("-");
    const answers = [
      { state: "created", email: "kim@agency.example", temporary_password: PW },
      { state: "already_claimed", email: "kim@agency.example" },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(answers.shift())));
    const post = () => claimPost(request("/join/claim", { method: "POST", cookie: inviteCookieHeader(), headers: SAME_ORIGIN }));
    const [first, second] = [await post(), await post()];
    // The browser applies both answers' cookies in order: the second sets only the notice cookie.
    const jar: Record<string, string> = {};
    for (const res of [first, second]) {
      for (const name of [names.claim, names.notice]) {
        const v = setCookie(res, name);
        if (v !== undefined) jar[name] = v;
      }
    }
    expect(setCookie(second, names.claim)).toBeUndefined();
    expect(shownAnswer(cfg, jarOf(jar), TOKEN)).toEqual({ state: "created", email: "kim@agency.example", temporaryPassword: PW });
  });

  it("claimStep", () => {
    expect(claimStep({ state: "created", email: "a@b.io", temporaryPassword: "p" })).toBe("keep");
    expect(claimStep({ state: "existing", email: "a@b.io", temporaryPassword: null })).toBe("ask");
    expect(claimStep(null)).toBe("ask");
  });

  it("sends API refusals back to the page as codes, and needs the invitation cookie", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "invitation_expired" }, { status: 410 })));
    const res = await claimPost(request("/join/claim", { method: "POST", cookie: inviteCookieHeader(), headers: SAME_ORIGIN }));
    expect(res.headers.get("location")).toBe(`${ORIGIN}/join/invite?error=invitation_expired`);
    const none = await claimPost(request("/join/claim", { method: "POST", headers: SAME_ORIGIN }));
    expect(none.headers.get("location")).toBe(`${ORIGIN}/join/invite?error=no_invitation`);
  });

  it("claimInvitation handles an unreachable API and a malformed answer", async () => {
    expect(await claimInvitation(cfg, TOKEN, vi.fn(async () => Promise.reject(new Error("down"))) as unknown as typeof fetch)).toEqual({
      ok: false,
      status: 0,
      detail: "api_unreachable",
    });
    const bad = await claimInvitation(cfg, TOKEN, vi.fn(async () => Response.json({ state: "weird" })) as unknown as typeof fetch);
    expect(bad).toEqual({ ok: false, status: 502, detail: "bad_claim_answer" });
    const existing = await claimInvitation(cfg, TOKEN, vi.fn(async () => Response.json({ state: "existing", email: "a@b.io" })) as unknown as typeof fetch);
    expect(existing).toEqual({ ok: true, claim: { state: "existing", email: "a@b.io", temporaryPassword: null } });
  });
});

describe("POST /join/confirm", () => {
  it("accepts with the session token, clears both cookies and switches the session to the new agency", async () => {
    const fetchSpy = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe(`${ORIGIN}/crm/v1/invitations/accept`);
      const h = new Headers(init.headers);
      expect(h.get("authorization")).toMatch(/^Bearer /);
      expect(h.get("x-aa-tenant-id")).toBeNull();
      expect(JSON.parse(String(init.body))).toEqual({ token: TOKEN });
      return Response.json({ tenant_id: "tenant-new", member_id: UUID, member_role: "agent" });
    });
    vi.stubGlobal("fetch", fetchSpy);
    const res = await confirmPost(
      request("/join/confirm", { method: "POST", cookie: `${signedIn(null)}; ${inviteCookieHeader()}`, headers: SAME_ORIGIN }),
    );
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe(`${ORIGIN}/`);
    const sets = res.headers.getSetCookie();
    expect(sets.filter((c) => /aa_crm_inv/.test(c)).every((c) => /Max-Age=0/i.test(c))).toBe(true);
    const jar = new Map(cookieHeader(res).split("; ").map((p) => [p.slice(0, p.indexOf("=")), { value: p.slice(p.indexOf("=") + 1) }]));
    expect(readSession(cfg, { get: (n: string) => jar.get(n) })?.tid).toBe("tenant-new");
  });

  it("AA-979: a Content studio invitation keeps the session's agency and says whether the studio is ready", async () => {
    for (const status of ["activated", "accepted"]) {
      vi.stubGlobal("fetch", vi.fn(async () => Response.json({ kind: "studio", tenant_id: "cis-tenant", status })));
      const res = await confirmPost(
        request("/join/confirm", { method: "POST", cookie: `${signedIn("tenant-a")}; ${inviteCookieHeader()}`, headers: SAME_ORIGIN }),
      );
      expect(res.status).toBe(303);
      expect(res.headers.get("location")).toBe(`${ORIGIN}/join/accept?studio=${status}`);
      const sets = res.headers.getSetCookie();
      expect(sets.filter((c) => /aa_crm_inv/.test(c)).every((c) => /Max-Age=0/i.test(c))).toBe(true);
      expect(sets.some((c) => /aa_crm_s=/.test(c))).toBe(false); // the CRM session (and its agency) is untouched
    }
  });

  it("refuses cross-site, needs a session, needs the invitation, and words API refusals", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "invitation_identity_mismatch" }, { status: 403 })));
    const cross = await confirmPost(request("/join/confirm", { method: "POST", cookie: signedIn(), headers: { "sec-fetch-site": "cross-site" } }));
    expect(cross.status).toBe(403);
    const anon = await confirmPost(request("/join/confirm", { method: "POST", cookie: inviteCookieHeader(), headers: SAME_ORIGIN }));
    expect(anon.headers.get("location")).toContain("/auth/refresh");
    const noInvite = await confirmPost(request("/join/confirm", { method: "POST", cookie: signedIn(), headers: SAME_ORIGIN }));
    expect(noInvite.headers.get("location")).toBe(`${ORIGIN}/join/accept?error=no_invitation`);
    const mismatch = await confirmPost(
      request("/join/confirm", { method: "POST", cookie: `${signedIn()}; ${inviteCookieHeader()}`, headers: SAME_ORIGIN }),
    );
    expect(mismatch.headers.get("location")).toBe(`${ORIGIN}/join/accept?error=invitation_identity_mismatch`);
  });
});

describe("routing and allow-list", () => {
  it("serves the invitation landing without a session, but not the accept step", () => {
    for (const p of ["/join", "/join/invite", "/join/claim"]) expect(isPublicPartnerPath(p)).toBe(true);
    for (const p of ["/join/accept", "/join/confirm", "/settings/team", "/join/other"]) expect(isPublicPartnerPath(p)).toBe(false);
  });

  it("lists every Team route with the agency, and the accept without one", () => {
    for (const [m, p] of [
      ["GET", "team"],
      ["POST", "team/invitations"],
      ["DELETE", `team/invitations/${UUID}`],
      ["PATCH", `team/members/${UUID}`],
      ["POST", `team/members/${UUID}/remove`],
      ["GET", "team/reassign-queue"],
      ["POST", "team/reassign"],
      ["GET", "plan"],
      ["GET", "recovery"],
      ["POST", `recovery/${UUID}/cancel`],
    ] as const) {
      expect(matchBff(m, p)?.tenant, `${m} ${p}`).toBe(true);
    }
    expect(matchBff("POST", "invitations/accept")?.tenant).toBe(false);
    expect(matchBff("DELETE", "team")).toBeNull();
    expect(matchBff("PATCH", "team/members/not-a-uuid")).toBeNull();
    expect(matchBff("POST", "invitations/claim")).toBeNull(); // the claim is server to server only
  });
});

describe("team helpers and strings", () => {
  const member = (over: Partial<TeamMember>): TeamMember => ({
    member_id: UUID,
    role: "agent",
    status: "active",
    email: null,
    joined_at: null,
    open_trips: 0,
    is_me: false,
    ...over,
  });

  it("names, targets and seats", () => {
    expect(memberLabel(member({ email: "lan@agency.example" }))).toBe("lan@agency.example");
    expect(memberLabel(member({}))).toBe("#00000000");
    const list = [member({ member_id: "a" }), member({ member_id: "b", status: "disabled" }), member({ member_id: "c" })];
    expect(reassignTargets(list, "a").map((m) => m.member_id)).toEqual(["c"]);
    expect(seatsLeft({ plan: "free", member_limit: 10, allowed_features: [], seats_used: 12 })).toBe(0);
    expect(seatsLeft({ plan: "free", member_limit: 10, allowed_features: [], seats_used: 3 })).toBe(7);
    expect(teamError("last_owner")).toBe("last_owner");
    expect(teamError("nope")).toBeNull();
  });

  it("has every AA-939 string in English and Vietnamese, with the placeholders kept", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => /^(join|team|recovery)\.|^settings\.team/.test(k));
    expect(keys.length).toBeGreaterThan(60);
    for (const k of keys) {
      const en = MESSAGES.en[k as keyof typeof MESSAGES.en];
      const vi = MESSAGES.vi[k as keyof typeof MESSAGES.vi];
      expect(vi, k).toBeTruthy();
      expect(vi.match(/\{\w+\}/g)?.sort(), k).toEqual(en.match(/\{\w+\}/g)?.sort());
    }
    for (const code of ["invitation_not_found", "invitation_expired", "invitation_used", "invitation_not_claimed",
      "invitation_identity_mismatch", "account_setup_pending", "no_invitation", "failed"]) {
      expect(MESSAGES.en[`join.error.${code}` as keyof typeof MESSAGES.en]).toBeTruthy();
    }
  });
});
