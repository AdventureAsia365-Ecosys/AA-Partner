// AA-979 P2b: the studio sign-in's pure helpers (lib/studioSession.ts). Run: npm test (from web/).
import { test } from "vitest";
import assert from "node:assert/strict";
import {
  boundTo,
  exchange,
  isPartnerToken,
  memberships,
  needsRenewal,
  PARTNER_REF_COOKIE,
  partnerRef,
  safeStudioNext,
  studioClaims,
  studioCookies,
  STUDIO_COOKIES,
} from "@/lib/studio/studioSession";

function jwt(claims: Record<string, unknown>): string {
  const b = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b({ alg: "HS256" })}.${b(claims)}.sig`;
}

const NOW = 1_800_000_000;

test("claims and renewal: partner tokens renew a minute before they expire; unreadable tokens always renew", () => {
  const partner = jwt({ sub: "t1", amr: "partner", exp: NOW + 600 });
  assert.equal(isPartnerToken(partner), true);
  assert.equal(isPartnerToken(jwt({ sub: "t1", exp: NOW + 600 })), false); // an API-key session
  assert.deepEqual(studioClaims(partner), { exp: NOW + 600, amr: "partner", sub: "t1", csr: null });
  assert.equal(studioClaims(jwt({ sub: "t1", amr: "partner", exp: NOW + 600, csr: "viewer" })).csr, "viewer");
  assert.equal(needsRenewal(partner, NOW), false);
  assert.equal(needsRenewal(jwt({ amr: "partner", exp: NOW + 30 }), NOW), true);
  assert.equal(needsRenewal("garbage", NOW), true);
  assert.equal(needsRenewal(undefined, NOW), true);
  assert.deepEqual(studioClaims("a.###.c"), { exp: null, amr: null, sub: null, csr: null });
});

test("the studio cookies live as long as the token and carry the partner reference", () => {
  const session = {
    token: "tok", tenant_id: "t1", tenant_name: "Sen Vàng", plan_tier: "growth", content_studio_role: "editor", expires_in: 840,
  };
  const cookies = studioCookies(session, "0123456789abcdef");
  assert.deepEqual(cookies.map((c) => c.name), [...STUDIO_COOKIES]);
  assert.ok(cookies.every((c) => c.maxAge === 840));
  assert.equal(cookies.find((c) => c.name === "cis_tenant_token")?.value, "tok");
  assert.equal(cookies.find((c) => c.name === PARTNER_REF_COOKIE)?.value, "0123456789abcdef");
  // without a reference (no partner session) the reference cookie is not written
  assert.ok(!studioCookies(session).some((c) => c.name === PARTNER_REF_COOKIE));
});

test("partnerRef: 16 hex, stable for one sign-in, different for another person or pool", async () => {
  const a = await partnerRef("https://idp.example.test/pool-a", "sub-1");
  assert.match(a, /^[0-9a-f]{16}$/);
  assert.equal(await partnerRef("https://idp.example.test/pool-a", "sub-1"), a);
  assert.notEqual(await partnerRef("https://idp.example.test/pool-a", "sub-2"), a);
  assert.notEqual(await partnerRef("https://idp.example.test/pool-b", "sub-1"), a);
  assert.ok(!a.includes("sub-1"));
});

test("boundTo: only the same reference counts", () => {
  assert.equal(boundTo("0123456789abcdef", "0123456789abcdef"), true);
  assert.equal(boundTo("fedcba9876543210", "0123456789abcdef"), false);
  assert.equal(boundTo(undefined, "0123456789abcdef"), false); // cookies from before the reference existed
  assert.equal(boundTo("", "0123456789abcdef"), false);
});

test("next paths stay inside the studio", () => {
  assert.equal(safeStudioNext("/portal/content?piece=1"), "/portal/content?piece=1");
  for (const bad of [null, "", "/", "//evil.example", "https://evil.example/portal/x", "/portal-access/select", "/portal\\x", "/auth/sign-out"]) {
    assert.equal(safeStudioNext(bad), "/portal/dashboard", String(bad));
  }
});

function fakeFetch(status: number, body: unknown, seen: unknown[] = []): typeof fetch {
  return (async (url: unknown, init?: RequestInit) => {
    seen.push([url, init]);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

test("exchange: the access token as Bearer, the chosen tenant in the body, every CIS answer mapped", async () => {
  const seen: unknown[] = [];
  const ok = await exchange("https://cis.example.test", "access", "t2", fakeFetch(200, { token: "tok", tenant_id: "t2", tenant_name: "A", plan_tier: "g", content_studio_role: "viewer", expires_in: 900 }, seen));
  assert.equal(ok.ok, true);
  const [url, init] = seen[0] as [string, RequestInit];
  assert.equal(url, "https://cis.example.test/auth/tenant-session");
  assert.equal(new Headers(init.headers).get("authorization"), "Bearer access");
  assert.deepEqual(JSON.parse(String(init.body)), { tenant_id: "t2" });
  const cases: Array<[number, unknown, string]> = [
    [409, { detail: "choose_tenant" }, "choose_tenant"],
    [403, { detail: "no_membership" }, "no_membership"],
    [403, { detail: "tenant_not_member" }, "tenant_not_member"],
    [403, { detail: "forbidden" }, "unauthorized"],
    [401, { detail: "unauthorized" }, "unauthorized"],
    [503, { detail: "unavailable" }, "unavailable"],
    [200, { nope: true }, "unavailable"],
  ];
  for (const [status, body, reason] of cases) {
    const r = await exchange("https://cis.example.test", "access", null, fakeFetch(status, body));
    assert.deepEqual(r, { ok: false, reason }, `${status} ${JSON.stringify(body)}`);
  }
  const down = await exchange("https://cis.example.test", "a", null, (async () => { throw new Error("down"); }) as typeof fetch);
  assert.deepEqual(down, { ok: false, reason: "unavailable" });
});

test("memberships: the list, or null when CIS refuses or is down", async () => {
  const list = [{ tenant_id: "t1", tenant_name: "A", content_studio_role: "owner", membership_id: "m1" }];
  assert.deepEqual(await memberships("https://cis.example.test", "a", fakeFetch(200, { memberships: list })), list);
  assert.equal(await memberships("https://cis.example.test", "a", fakeFetch(403, {})), null);
  assert.equal(await memberships("https://cis.example.test", "a", fakeFetch(200, { memberships: "x" })), null);
});
