// AA-1025 (S264): the CRM sign-in shown by the studio (lib/crmIdentity.ts), and the guard that the support address is
// never rendered bare on the access pages. Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { CRM_ME_PATH, fetchCrmIdentity, readCrmIdentity, signedInText } from "./crmIdentity.ts";

const ROOT = path.resolve(import.meta.dirname, "..");

test("an agency answer gives the agency name and the role", () => {
  const id = readCrmIdentity(200, { tenant_id: "t-1", member_id: "m-1", member_role: "agent", display_name: "Lotus Trail Travel" });
  assert.deepEqual(id, { kind: "agency", agencyName: "Lotus Trail Travel", role: "agent" });
  assert.equal(signedInText(id), "Signed in to AA Partner as Agent of Lotus Trail Travel.");
});

test("an unknown role keeps the agency name and drops the role", () => {
  const id = readCrmIdentity(200, { member_role: "superuser", display_name: "  Lotus Trail Travel  " });
  assert.deepEqual(id, { kind: "agency", agencyName: "Lotus Trail Travel", role: null });
  assert.equal(signedInText(id), "Signed in to AA Partner for Lotus Trail Travel.");
});

test("no agency name never falls back to the agency id", () => {
  assert.equal(readCrmIdentity(200, { tenant_id: "t-1", member_role: "owner", display_name: null }), null);
  assert.equal(readCrmIdentity(200, { tenant_id: "t-1", member_role: "owner", display_name: "   " }), null);
});

test("a session without an agency says so", () => {
  const id = readCrmIdentity(409, { detail: "no_agency_selected" });
  assert.deepEqual(id, { kind: "no_agency" });
  assert.equal(signedInText(id), "Signed in to AA Partner, with no agency selected.");
});

test("anything else shows nothing", () => {
  for (const [status, body] of [
    [401, { detail: "session_expired" }],
    [404, { detail: "not_found" }],
    [409, { detail: "other" }],
    [500, null],
    [200, null],
    [200, "text"],
  ] as const) {
    const id = readCrmIdentity(status, body);
    assert.equal(id, null, `${status}`);
    assert.equal(signedInText(id), null);
  }
});

test("a very long agency name is cut", () => {
  const id = readCrmIdentity(200, { member_role: "owner", display_name: "x".repeat(500) });
  assert.equal(id?.kind === "agency" ? id.agencyName.length : 0, 120);
});

test("fetchCrmIdentity asks the CRM web on this origin with the session cookie", async () => {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fake = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ member_role: "owner", display_name: "Lotus Trail Travel" }), { status: 200 });
  }) as unknown as typeof fetch;
  const id = await fetchCrmIdentity(fake);
  assert.deepEqual(id, { kind: "agency", agencyName: "Lotus Trail Travel", role: "owner" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, CRM_ME_PATH);
  assert.equal(calls[0].init?.credentials, "same-origin");
});

test("fetchCrmIdentity never throws (network error, HTML answer)", async () => {
  const down = (async () => {
    throw new Error("offline");
  }) as unknown as typeof fetch;
  assert.equal(await fetchCrmIdentity(down), null);
  const html = (async () => new Response("<html>sign in</html>", { status: 200 })) as unknown as typeof fetch;
  assert.equal(await fetchCrmIdentity(html), null);
});

test("the access pages label the support address as a contact and show the CRM sign-in", () => {
  for (const rel of ["app/portal-access/none/page.tsx", "app/portal-access/select/page.tsx"]) {
    const code = fs.readFileSync(path.join(ROOT, rel), "utf8");
    assert.match(code, /<SupportContact \/>/, `${rel}: support address via SupportContact`);
    assert.match(code, /<SignedInAs \/>/, `${rel}: CRM sign-in shown`);
    assert.doesNotMatch(code, /SUPPORT_EMAIL/, `${rel}: no bare support address`);
  }
  const card = fs.readFileSync(path.join(ROOT, "app/portal-access/AccessCard.tsx"), "utf8");
  assert.match(card, /Questions\? Contact Adventure Asia:/);
});
