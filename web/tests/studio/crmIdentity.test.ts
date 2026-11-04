// AA-1025 (S264): the CRM sign-in shown by the studio (lib/studio/crmIdentity.ts), and the guard that the support address is
// never rendered bare on the access pages. Run: npm test (from web/).
import { test } from "vitest";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { CRM_IDENTITY_PATH, fetchCrmIdentity, readCrmIdentity, signedInText } from "@/lib/studio/crmIdentity";

const ROOT = path.resolve(import.meta.dirname, "../..");

test("an agency answer gives the agency name and the role", () => {
  const id = readCrmIdentity(200, { kind: "agency", member_role: "agent", display_name: "Lotus Trail Travel" });
  assert.deepEqual(id, { kind: "agency", agencyName: "Lotus Trail Travel", role: "agent", email: null });
  assert.equal(signedInText(id), "Signed in to AA Partner as Agent of Lotus Trail Travel.");
});

test("an unknown role keeps the agency name and drops the role", () => {
  const id = readCrmIdentity(200, { kind: "agency", member_role: "superuser", display_name: "  Lotus Trail Travel  " });
  assert.deepEqual(id, { kind: "agency", agencyName: "Lotus Trail Travel", role: null, email: null });
  assert.equal(signedInText(id), "Signed in to AA Partner for Lotus Trail Travel.");
  for (const role of ["constructor", "toString", "__proto__"]) {
    assert.deepEqual(readCrmIdentity(200, { kind: "agency", member_role: role, display_name: "Lotus Trail Travel" }), {
      kind: "agency",
      agencyName: "Lotus Trail Travel",
      role: null,
      email: null,
    });
  }
});

test("no agency name never falls back to the agency id", () => {
  assert.equal(readCrmIdentity(200, { kind: "agency", member_role: "owner", display_name: null }), null);
  assert.equal(readCrmIdentity(200, { kind: "agency", member_role: "owner", display_name: "   " }), null);
});

test("a session without an agency says so", () => {
  const id = readCrmIdentity(200, { kind: "no_agency" });
  assert.deepEqual(id, { kind: "no_agency", email: null });
  assert.equal(signedInText(id), "Signed in to AA Partner, with no agency selected.");
});

// AA-1036 (an AA-1007 leftover): the studio shows the signed-in person's own e-mail.
test("the person's own e-mail is shown when the session knows it", () => {
  const id = readCrmIdentity(200, { kind: "agency", member_role: "owner", display_name: "Lotus Trail Travel", email: "mira.solano@example.test" });
  assert.equal(signedInText(id), "Signed in to AA Partner as mira.solano@example.test (Owner of Lotus Trail Travel).");
  const noRole = readCrmIdentity(200, { kind: "agency", member_role: null, display_name: "Lotus Trail Travel", email: "mira.solano@example.test" });
  assert.equal(signedInText(noRole), "Signed in to AA Partner as mira.solano@example.test, for Lotus Trail Travel.");
  const none = readCrmIdentity(200, { kind: "no_agency", email: "mira.solano@example.test" });
  assert.equal(signedInText(none), "Signed in to AA Partner as mira.solano@example.test, with no agency selected.");
  for (const bad of ["no-at-sign", 7, `${"a".repeat(260)}@example.test`]) {
    const id2 = readCrmIdentity(200, { kind: "no_agency", email: bad });
    assert.equal(id2?.kind === "no_agency" ? id2.email : "x", null);
  }
});

test("anything else shows nothing", () => {
  for (const [status, body] of [
    [200, { kind: "signed_out" }],
    [200, { kind: "unknown" }],
    [200, { kind: "something_new" }],
    // the old /bff/me shapes: no `kind`, so nothing is shown
    [200, { tenant_id: "t-1", member_role: "owner", display_name: "Lotus Trail Travel" }],
    [409, { detail: "no_agency_selected" }],
    [401, { detail: "session_expired" }],
    [404, { detail: "not_found" }],
    [404, { kind: "agency", member_role: "owner", display_name: "Lotus Trail Travel" }],
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
  const id = readCrmIdentity(200, { kind: "agency", member_role: "owner", display_name: "x".repeat(500) });
  assert.equal(id?.kind === "agency" ? id.agencyName.length : 0, 120);
});

test("fetchCrmIdentity asks the CRM web on this origin with the session cookie", async () => {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fake = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ kind: "agency", member_role: "owner", display_name: "Lotus Trail Travel" }), { status: 200 });
  }) as unknown as typeof fetch;
  const id = await fetchCrmIdentity(fake);
  assert.deepEqual(id, { kind: "agency", agencyName: "Lotus Trail Travel", role: "owner", email: null });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, CRM_IDENTITY_PATH);
  assert.equal(CRM_IDENTITY_PATH, "/bff/identity"); // never /bff/me: its 401 / 409 are console errors on the studio
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
  for (const rel of ["app/(studio)/portal-access/none/page.tsx", "app/(studio)/portal-access/select/page.tsx"]) {
    const code = fs.readFileSync(path.join(ROOT, rel), "utf8");
    assert.match(code, /<SupportContact \/>/, `${rel}: support address via SupportContact`);
    assert.match(code, /<SignedInAs \/>/, `${rel}: CRM sign-in shown`);
    assert.doesNotMatch(code, /SUPPORT_EMAIL/, `${rel}: no bare support address`);
  }
  // AA-1036 (R3): the label lives in the access pages' message catalogue (EN + VI); the card renders it.
  const card = fs.readFileSync(path.join(ROOT, "app/(studio)/portal-access/AccessCard.tsx"), "utf8");
  assert.match(card, /t\("support"\)/);
  const messages = fs.readFileSync(path.join(ROOT, "app/(studio)/portal-access/access.messages.ts"), "utf8");
  assert.match(messages, /Questions\? Contact Adventure Asia:/);
});
