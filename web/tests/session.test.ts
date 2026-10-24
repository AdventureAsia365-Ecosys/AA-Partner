// The server session: sealed cookies (AES-GCM, per-purpose keys), host-only names, expiry, and config validation.
import { describe, expect, it } from "vitest";
import { loadConfig } from "@/lib/config";
import { open, seal } from "@/lib/seal";
import {
  accessValid,
  clearedCookies,
  cookieNames,
  readRefresh,
  readSession,
  refreshCookie,
  sessionCookie,
  txCookie,
} from "@/lib/session";
import { ENV, testConfig } from "./helpers";

const SECRET = ENV.WEB_SESSION_SECRET;

function jar(...specs: { name: string; value: string }[]) {
  const m = new Map(specs.map((s) => [s.name, { value: s.value }]));
  return { get: (n: string) => m.get(n) };
}

describe("seal", () => {
  it("round-trips and refuses tampering, another purpose, another key and expiry", () => {
    const now = 1_000_000;
    const sealed = seal(SECRET, "session", { a: "Lữ hành", exp: now + 60 });
    expect(open<{ a: string; exp: number }>(SECRET, "session", sealed, now)?.a).toBe("Lữ hành");
    expect(open(SECRET, "refresh", sealed, now)).toBeNull();
    expect(open(`${SECRET}x`, "session", sealed, now)).toBeNull();
    expect(open(SECRET, "session", sealed, now + 61)).toBeNull();
    const bytes = Buffer.from(sealed, "base64url");
    bytes[bytes.length - 20] ^= 1;
    expect(open(SECRET, "session", bytes.toString("base64url"), now)).toBeNull();
    expect(open(SECRET, "session", "garbage", now)).toBeNull();
    expect(open(SECRET, "session", undefined, now)).toBeNull();
    expect(seal(SECRET, "session", { exp: now + 1 })).not.toBe(seal(SECRET, "session", { exp: now + 1 }));
  });
});

describe("session cookies", () => {
  const cfg = testConfig();

  it("are host-only, prefixed, httpOnly, Secure and scoped (refresh only to /auth)", () => {
    const names = cookieNames(cfg);
    expect(names.session).toBe("__Host-aa_crm_s");
    expect(names.refresh).toBe("__Secure-aa_crm_r");
    const s = sessionCookie(cfg, { at: "t", atExp: 1, iss: "i", sub: "s", cid: "c", tid: null });
    expect(s).toMatchObject({ httpOnly: true, secure: true, sameSite: "lax", path: "/" });
    expect(refreshCookie(cfg, "rt", "s").path).toBe("/auth");
    expect(txCookie(cfg, { state: "a", verifier: "b", nonce: "c", next: "/" }).maxAge).toBe(600);
    expect(clearedCookies(cfg).every((c) => c.maxAge === 0 && c.value === "")).toBe(true);
  });

  it("are readable back and the access token validity has a skew", () => {
    const now = Math.floor(Date.now() / 1000);
    const s = sessionCookie(cfg, { at: "tok", atExp: now + 120, iss: "i", sub: "s", cid: "c", tid: "t1" });
    const r = refreshCookie(cfg, "rt", "s");
    const session = readSession(cfg, jar(s));
    expect(session).toMatchObject({ at: "tok", tid: "t1", sub: "s" });
    expect(accessValid(session)).toBe(true);
    expect(accessValid(session, now + 61)).toBe(false);
    expect(accessValid(null)).toBe(false);
    expect(readRefresh(cfg, jar(r))?.rt).toBe("rt");
    expect(readSession(cfg, jar({ name: cookieNames(cfg).session, value: r.value }))).toBeNull();
  });

  it("drop the prefixes and Secure only for http://localhost development", () => {
    const local = loadConfig({ ...ENV, ENVIRONMENT: "local", WEB_PUBLIC_ORIGIN: "http://localhost:3005" });
    expect(local.secureCookies).toBe(false);
    expect(cookieNames(local).session).toBe("aa_crm_s");
    const devHttp = loadConfig({ ...ENV, WEB_PUBLIC_ORIGIN: "http://partner.example.test" });
    expect(devHttp.authConfigured).toBe(false);
    expect(devHttp.problems).toContain("WEB_PUBLIC_ORIGIN");
  });
});

describe("configuration", () => {
  it("is complete with every value and names what is missing otherwise", () => {
    expect(loadConfig(ENV).authConfigured).toBe(true);
    const missing = loadConfig({ ...ENV, COGNITO_CLIENT_SECRET: "", WEB_SESSION_SECRET: "short", COGNITO_CLIENT_ID: "bad id" });
    expect(missing.authConfigured).toBe(false);
    expect(missing.problems).toEqual(expect.arrayContaining(["COGNITO_CLIENT_SECRET", "WEB_SESSION_SECRET", "COGNITO_CLIENT_ID"]));
    expect(loadConfig({ ...ENV, ENVIRONMENT: "weird" }).problems).toContain("ENVIRONMENT");
    expect(loadConfig({ ...ENV, COGNITO_DOMAIN: "https://auth.example.test/path" }).problems).toContain("COGNITO_DOMAIN");
    expect(loadConfig({ ...ENV, WEB_PARTNER_HOSTS: "" }).partnerHosts).toEqual(["partner.example.test"]);
    expect(loadConfig({ ...ENV, WEB_CLIENT_HOSTS: "partner.example.test,go.example.test" }).clientHosts).toEqual(["go.example.test"]);
    expect(loadConfig({}).authConfigured).toBe(false);
  });
});
