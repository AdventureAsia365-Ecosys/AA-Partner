// AA-979 P2b: the studio's configuration (lib/config.ts). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { hostAllowed, loadConfig } from "./config.ts";

const SECRET = "s".repeat(40);

test("Fargate: hosts, public origin, partner sign-in, API-key sign-in until the clean-up", () => {
  const cfg = loadConfig({
    ENVIRONMENT: "dev",
    PORTAL_HOSTS: "Portal.Example.test",
    PORTAL_PUBLIC_ORIGIN: "https://portal.example.test",
    WEB_SESSION_SECRET: SECRET,
  });
  assert.equal(cfg.publicOrigin, "https://portal.example.test");
  assert.deepEqual(cfg.hosts, ["portal.example.test"]);
  assert.equal(cfg.partnerSignIn, true);
  assert.equal(cfg.apiKeySignIn, true);
  assert.equal(cfg.signInPath, "/auth/refresh");
  assert.equal(cfg.signOutPath, "/auth/sign-out");
  assert.equal(cfg.secureCookies, true);
  assert.equal(hostAllowed(cfg, "portal.example.test"), true);
  assert.equal(hostAllowed(cfg, "10.0.1.5:3000"), false);
  assert.equal(loadConfig({ PORTAL_API_KEY_SIGNIN: "FALSE" }).apiKeySignIn, false);
});

test("Vercel: nothing set = the P2a portal (any host, request origin, API key only)", () => {
  const cfg = loadConfig({});
  assert.equal(cfg.publicOrigin, "");
  assert.equal(cfg.partnerSignIn, false);
  assert.equal(cfg.apiKeySignIn, true);
  assert.equal(hostAllowed(cfg, "aa-cis-portal.vercel.app"), true);
});

test("bad values are refused, never used", () => {
  const cfg = loadConfig({
    PORTAL_PUBLIC_ORIGIN: "http://portal.example.test/x",
    WEB_SESSION_SECRET: "short",
    CRM_WEB_SIGN_IN_PATH: "https://evil.example/",
    ENVIRONMENT: "weird",
  });
  assert.equal(cfg.publicOrigin, "");
  assert.equal(cfg.partnerSignIn, false);
  assert.equal(cfg.signInPath, "/auth/refresh");
  assert.equal(cfg.environment, "dev");
  assert.equal(loadConfig({ ENVIRONMENT: "local", PORTAL_PUBLIC_ORIGIN: "http://localhost:3004" }).secureCookies, false);
});
