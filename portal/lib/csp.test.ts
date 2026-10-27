// AA-979 P2b (S253): the studio CSP (lib/csp.ts) and the managed-login origin it allows for the sign-out redirect.
// Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "./config.ts";
import { contentSecurityPolicy } from "./csp.ts";

test("Fargate: form-action allows this origin and the managed-login origin the sign-out redirects to", () => {
  const cfg = loadConfig({ CRM_SIGN_OUT_FORM_ORIGIN: "https://auth.example.test" });
  assert.equal(cfg.signOutFormOrigin, "https://auth.example.test");
  const csp = contentSecurityPolicy(cfg.signOutFormOrigin);
  assert.match(csp, /form-action 'self' https:\/\/auth\.example\.test;/);
  for (const d of ["frame-ancestors 'none'", "base-uri 'self'", "object-src 'none'"]) assert.ok(csp.includes(d), d);
});

test("Vercel: nothing set = forms to this origin only (the AA-885 policy, unchanged)", () => {
  assert.equal(
    contentSecurityPolicy(loadConfig({}).signOutFormOrigin),
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
  );
});

test("a bad form origin is refused, never added to the policy", () => {
  for (const bad of ["http://auth.example.test", "https://auth.example.test/logout", "javascript:x", "*", "'unsafe-inline'"]) {
    const cfg = loadConfig({ CRM_SIGN_OUT_FORM_ORIGIN: bad });
    assert.equal(cfg.signOutFormOrigin, "", bad);
    assert.match(contentSecurityPolicy(cfg.signOutFormOrigin), /form-action 'self';/);
  }
});
