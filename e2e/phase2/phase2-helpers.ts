// e2e/phase2/phase2-helpers.ts — AA-979 P2b phase 2 (re-enrolment + studio on the agency host): configuration, the
// evidence folder, the local credential file, screenshots and the browser steps shared by phase2.spec.ts.
//
// Every host comes from the environment (no host of a real environment in this public repo). Names have no
// underscores (the workspace rule for hand-typed variables). See playwright.phase2.config.ts for the list.
//
// Secrets: the invitee's own password and authenticator secret exist only in this process and in ONE local file,
// <OUT>/.private/<hash>.json (0600, folder 0700), so the orchestrator can sign the same person in again (role checks,
// rollback drill, a rerun with E2EP2REUSE=1). OUT must be outside the repository or under e2e/results/ (gitignored);
// nothing secret is logged, attached or screenshotted (the managed login pages and the claimed-invitation page, which
// shows the one-time password, are never captured).
import { expect, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { baseline, waitForCode } from "../helpers/mailbox";
import { signIn, skipPasskeyOffer } from "../helpers/signin";
import { freshTotp, parseSetupSecret } from "../helpers/totp";

export type StaffMode = "none" | "browser" | "a10b";
export type StudioRole = "owner" | "editor" | "viewer";

export interface Phase2Config {
  portal: string; // the agency host origin (portal.)
  cf: string; // the crm_agency CloudFront domain the portal host is pinned to ("" after the cutover)
  cisApi: string; // the CIS API origin
  tenant: string; // the synthetic CIS tenant id
  invitee: string; // the invited address (an e2e mailbox recipient)
  role: StudioRole;
  partner: string; // expected origin of the join link ("" = whatever the mail says)
  staff: StaffMode;
  admin: string; // the CIS admin origin (staff modes browser / a10b)
  a10bEmail: string; // the Dev-only E2E CIS admin address (staff mode a10b)
  since: number; // staff mode none: epoch ms before the orchestrator's invitation call
  out: string; // evidence folder (absolute)
  reuse: boolean; // skip (a) and (b): sign in with the credentials saved by an earlier run
  tenantKey: string; // optional: the tenant's API key, for the E check (same tenant, API-key session)
  readyMs: number; // how long "being set up" may last
}

const E2E_DIR = path.resolve(__dirname, "..");
const REPO_DIR = path.resolve(E2E_DIR, "..");

function env(name: string): string {
  return (process.env[name] ?? "").trim();
}

function origin(name: string, required: boolean): string {
  const v = env(name).replace(/\/+$/, "");
  if (!v) {
    if (required) throw new Error(`missing ${name}`);
    return "";
  }
  const u = new URL(v);
  if (u.protocol !== "https:" || u.pathname !== "/" || u.search) throw new Error(`${name} must be an https origin`);
  return u.origin;
}

/** The evidence folder: outside the repository, or under e2e/results/ (gitignored). */
export function outDir(raw = env("E2EP2OUT")): string {
  const out = path.resolve(E2E_DIR, raw || "results/phase2");
  const rel = path.relative(REPO_DIR, out);
  const inside = !rel.startsWith("..") && !path.isAbsolute(rel);
  if (inside && !(out + path.sep).startsWith(path.join(E2E_DIR, "results") + path.sep)) {
    throw new Error("E2EP2OUT inside the repository must be under e2e/results/ (gitignored)");
  }
  return out;
}

export function loadConfig(): Phase2Config {
  const staff = (env("E2EP2STAFF") || "none") as StaffMode;
  if (!["none", "browser", "a10b"].includes(staff)) throw new Error("E2EP2STAFF must be none, browser or a10b");
  const role = (env("E2EP2ROLE") || "viewer") as StudioRole;
  if (!["owner", "editor", "viewer"].includes(role)) throw new Error("E2EP2ROLE must be owner, editor or viewer");
  const tenant = env("E2EP2TENANT");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(tenant)) throw new Error("E2EP2TENANT must be a uuid");
  const invitee = env("E2EP2INVITEE").toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(invitee)) throw new Error("E2EP2INVITEE must be an address");
  const cf = env("E2EP2CF").toLowerCase();
  if (cf && !/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(cf)) throw new Error("E2EP2CF must be a host name");
  const reuse = env("E2EP2REUSE") === "1";
  const sinceRaw = env("E2EP2SINCE");
  const since = sinceRaw ? Date.parse(sinceRaw) : NaN;
  if (staff === "none" && !reuse && !Number.isFinite(since)) {
    throw new Error("E2EP2STAFF=none needs E2EP2SINCE (ISO time taken just before the invitation call)");
  }
  return {
    portal: origin("E2EP2PORTAL", true),
    cf,
    cisApi: origin("E2EP2CISAPI", true),
    tenant: tenant.toLowerCase(),
    invitee,
    role,
    partner: origin("E2EP2PARTNER", false),
    staff,
    admin: origin("E2EP2ADMIN", staff !== "none" && !reuse),
    a10bEmail: env("E2EP2A10BEMAIL").toLowerCase(),
    since,
    out: outDir(),
    reuse,
    tenantKey: env("E2EP2TENANTKEY"),
    readyMs: (Number(env("E2EP2READYMIN")) || 5) * 60_000,
  };
}

// ── run record (no secrets) and the local credential file ──────────────────────────────────────────────────────────

export function record(cfg: Phase2Config, patch: Record<string, unknown>): void {
  fs.mkdirSync(cfg.out, { recursive: true });
  const file = path.join(cfg.out, "run.json");
  let cur: Record<string, unknown> = {};
  try {
    cur = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    // first write
  }
  fs.writeFileSync(file, JSON.stringify({ ...cur, ...patch }, null, 2) + "\n");
}

export interface PersonCredentials {
  email: string;
  password?: string;
  totpSecret?: string;
}

function credentialFile(cfg: Phase2Config): string {
  const key = createHash("sha256").update(`aa979-phase2:${cfg.invitee}`).digest("hex").slice(0, 16);
  return path.join(cfg.out, ".private", `${key}.json`);
}

export function saveCredentials(cfg: Phase2Config, creds: PersonCredentials): void {
  const file = credentialFile(cfg);
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.writeFileSync(file, JSON.stringify({ ...creds, saved_at: new Date().toISOString() }), { mode: 0o600 });
  fs.chmodSync(file, 0o600);
}

export function loadCredentials(cfg: Phase2Config): PersonCredentials {
  const file = credentialFile(cfg);
  if (!fs.existsSync(file)) throw new Error("E2EP2REUSE=1 but no saved credentials for this invitee in E2EP2OUT/.private");
  const c = JSON.parse(fs.readFileSync(file, "utf8")) as PersonCredentials;
  if (!c.password || !c.totpSecret) throw new Error("the saved credentials are incomplete (the enrolment did not finish)");
  return c;
}

// ── screenshots ─────────────────────────────────────────────────────────────────────────────────────────────────────

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"]'), null, { timeout: 15_000 }).catch(() => {});
}

/** The portal scrolls inside <main data-pshell="main">: grow the desktop viewport to its content (capped). */
async function fitToMain(page: Page, width: number, minHeight: number): Promise<void> {
  const h = await page.evaluate(() => {
    const main = document.querySelector<HTMLElement>('[data-pshell="main"]');
    return main ? main.getBoundingClientRect().top + main.scrollHeight : 0;
  });
  await page.setViewportSize({ width, height: Math.min(Math.max(minHeight, Math.ceil(h)), 8000) });
}

/** 1440 (full height) + 390 (first screen) in the given colour schemes: <OUT>/<name>-<1440|390>-<scheme>.png. */
export async function shots(
  cfg: Phase2Config,
  page: Page,
  name: string,
  schemes: readonly ("light" | "dark")[] = ["light", "dark"],
): Promise<void> {
  fs.mkdirSync(cfg.out, { recursive: true });
  for (const scheme of schemes) {
    await page.emulateMedia({ colorScheme: scheme });
    if (scheme === "dark") await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page);
    for (const width of [1440, 390] as const) {
      const height = width === 1440 ? 900 : 844;
      await page.setViewportSize({ width, height });
      await page.waitForTimeout(300);
      if (width === 1440) await fitToMain(page, width, height);
      await page.screenshot({ path: path.join(cfg.out, `${name}-${width}-${scheme}.png`), fullPage: true });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1440, height: 900 });
}

// ── (a) staff side ──────────────────────────────────────────────────────────────────────────────────────────────────

/** A same-origin request from the page (the session cookie and the Origin header go with it; the BFF needs both). */
export async function sameOriginFetch(page: Page, method: string, url: string, body?: unknown) {
  return page.evaluate(
    async ({ method, url, body }) => {
      const res = await fetch(url, {
        method,
        headers: body === undefined ? {} : { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await res.text();
      let json: unknown = null;
      try {
        json = JSON.parse(text);
      } catch {
        // not JSON
      }
      return { status: res.status, json, text: text.slice(0, 500) };
    },
    { method, url, body },
  );
}

/** Staff mode "browser": a person signs in to the CIS admin (Cognito, own staff account, MFA) in the headed window. */
export async function waitForHumanStaffSignIn(page: Page, admin: string): Promise<void> {
  await page.goto(`${admin}/login`, { waitUntil: "domcontentloaded" });
  console.log(JSON.stringify({ event: "aa979_phase2_waiting_for_staff_sign_in", minutes: 5 }));
  await page.waitForURL((u) => u.origin === admin && u.pathname.startsWith("/admin"), { timeout: 300_000 });
}

/**
 * Staff mode "a10b": the Dev-only E2E CIS admin (root ADR 0006a A10b) signs in by e-mail code from the inbound-admin/
 * prefix (readable by the MFA admin role only: run with the orchestrator's aa365-admin profile). Same steps as AA-CIS-App
 * tests/e2e/helpers/cognitoSignIn.ts. NOTE: that identity is read-only (403 e2e_read_only on every staff write outside
 * its allow-list), so the invitation call is refused until the allow-list names the two invitation routes.
 */
export async function a10bStaffSignIn(page: Page, admin: string, email: string): Promise<void> {
  if (!email) throw new Error("E2EP2STAFF=a10b needs E2EP2A10BEMAIL");
  await page.goto(`${admin}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("link", { name: /sign in with company email/i }).click();
  await page.waitForURL((u) => u.origin !== admin, { timeout: 20_000 });
  const field = page.getByLabel(/email/i).first();
  await expect(field).toBeVisible({ timeout: 15_000 });
  await field.fill(email);
  const before = await baseline("inbound-admin/");
  await page.getByRole("button", { name: /^next$/i }).click({ timeout: 10_000 });
  const codeField = page.getByLabel(/verification code|sign-in code|^code$/i).first();
  await expect
    .poll(
      async () => {
        if (await codeField.isVisible().catch(() => false)) return "code";
        const choice = page
          .getByRole("button", { name: /email|one-time code/i })
          .or(page.getByRole("radio", { name: /email/i }))
          .first();
        if (await choice.isVisible().catch(() => false)) {
          await choice.click({ timeout: 5_000 });
          const next = page.getByRole("button", { name: /^(next|continue)$/i }).first();
          if (await next.isVisible().catch(() => false)) await next.click({ timeout: 5_000 }).catch(() => {});
        }
        return "waiting";
      },
      { timeout: 30_000, intervals: [500, 1_000, 2_000] },
    )
    .toBe("code");
  await codeField.fill(await waitForCode(email, before, 90_000, "inbound-admin/"));
  await page.getByRole("button", { name: /^(continue|sign in|next)$/i }).first().click({ timeout: 10_000 });
  await page.waitForURL((u) => u.origin === admin && u.pathname.startsWith("/admin"), { timeout: 30_000 });
}

// ── (b) the invitee: claim, managed login (new password + authenticator), accept ───────────────────────────────────

async function click(page: Page, name: RegExp): Promise<void> {
  await page.getByRole("button", { name }).first().click();
}

// S253: the managed login page renders the field before its script takes over, and a value typed in that window is
// dropped (Next then answers "Missing email address" and the old code waited 15 minutes for a password field).
// Fill once the page is idle, and fill again until the field holds the value.
async function fillSettled(page: Page, field: ReturnType<Page["getByLabel"]>, value: string): Promise<void> {
  await page.waitForLoadState("networkidle").catch(() => {});
  await expect(async () => {
    if ((await field.inputValue()) !== value) await field.fill(value);
    await expect(field).toHaveValue(value, { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
}

function newPassword(): string {
  // >= 14 characters with all four classes (the partner pool's policy).
  return `Aa1!${randomBytes(18).toString("base64url")}`;
}

/**
 * Opens the join link, claims (one-time password), signs in through managed login (own password, authenticator from
 * the "Show secret key" text), accepts. Returns the studio status the join page shows: "activated" (Content studio
 * access is ready) or "accepted" (being set up: the crm_studio_activation job retries).
 */
export async function claimEnrolAccept(
  cfg: Phase2Config,
  page: Page,
  link: string,
): Promise<{ status: "activated" | "accepted"; joinOrigin: string }> {
  const joinOrigin = new URL(link).origin;
  if (cfg.partner) expect(joinOrigin, "the join link host").toBe(cfg.partner);
  await page.goto(link);
  await page.waitForURL(`${joinOrigin}/join/invite`);
  expect(page.url()).not.toContain("t="); // the token left the address bar
  await shots(cfg, page, "p2-01-join-invite", ["light"]); // before the claim: no secret on screen
  await page.getByTestId("join-open").click();
  await expect(page.getByTestId("join-claimed")).toBeVisible();
  await expect(page.getByTestId("join-email")).toHaveText(cfg.invitee);
  const temporary = (await page.getByTestId("join-temporary-password").textContent({ timeout: 5_000 }).catch(() => ""))?.trim() ?? "";
  if (temporary.length < 14) {
    throw new Error(
      "no one-time password: the invitee already has a partner-pool account (reset it first: AA-CIS-Infra " +
        "tools/identity/crm_dev_identity.py invitee-reset --apply, or use E2EP2REUSE=1 with saved credentials)",
    );
  }
  await page.getByTestId("join-sign-in").click();

  const email = page.getByLabel(/email/i).first();
  await expect(email).toBeVisible({ timeout: 30_000 });
  await fillSettled(page, email, cfg.invitee);
  const password = page.getByLabel(/^password$/i).first();
  if (!(await password.isVisible().catch(() => false))) await click(page, /^(next|sign in|continue)$/i);
  await expect(password, "managed login did not show the password field after the e-mail").toBeVisible({ timeout: 30_000 });
  await password.fill(temporary);
  await click(page, /^(next|sign in|continue)$/i);
  const mine = newPassword();
  const fresh = page.getByLabel(/new password/i).first();
  await expect(fresh).toBeVisible({ timeout: 30_000 });
  await fresh.fill(mine);
  const confirm = page.getByLabel(/confirm|re-?enter/i).first();
  if (await confirm.isVisible().catch(() => false)) await confirm.fill(mine);
  await click(page, /^(next|change password|continue|submit|save)$/i);
  saveCredentials(cfg, { email: cfg.invitee, password: mine });
  // Authenticator setup (managed login v2; same selectors as tests/invite.spec.ts, checked on Dev in S249).
  await expect(page.getByRole("heading", { name: /authenticator/i })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: /show secret key|can.?t scan/i }).click();
  const keyLine = page.locator("p.qr-secret-key").or(page.getByText(/secret key:/i)).first();
  await expect(keyLine).toBeVisible({ timeout: 10_000 });
  const secret = parseSetupSecret(await keyLine.innerText());
  if (!secret) throw new Error("authenticator secret not found on the setup page");
  saveCredentials(cfg, { email: cfg.invitee, password: mine, totpSecret: secret });
  await page.getByLabel(/code/i).first().fill(await freshTotp(secret));
  await click(page, /^(sign in|next|verify|continue|submit|confirm)$/i);
  await skipPasskeyOffer(page);

  await page.waitForURL(`${joinOrigin}/join/accept`, { timeout: 60_000 });
  await page.getByTestId("join-accept").click();
  await page.waitForURL((u) => u.origin === joinOrigin && u.pathname === "/join/accept" && u.searchParams.has("studio"), {
    timeout: 30_000,
  });
  const status = new URL(page.url()).searchParams.get("studio") === "activated" ? "activated" : "accepted";
  await expect(page.getByTestId("join-studio-status")).toContainText(
    status === "activated" ? /access is ready/i : /being set up/i,
  );
  await shots(cfg, page, `p2-02-join-studio-${status}`, ["light"]);
  return { status, joinOrigin };
}

// ── (c) the studio on the agency host, signed in with the partner pool ─────────────────────────────────────────────

/**
 * Proves the agency host answers from the pinned distribution (a browser navigation: the resolver rule applies to the
 * browser only, never to Node requests): the portal /healthz through CloudFront. Returns its JSON (the image SHA).
 */
export async function pinnedHealth(cfg: Phase2Config, page: Page): Promise<Record<string, unknown>> {
  const res = await page.goto(`${cfg.portal}/healthz`);
  expect(res, "no answer from the agency host").not.toBeNull();
  const headers = res!.headers();
  expect(`${headers["via"] ?? ""} ${headers["x-cache"] ?? ""}`.toLowerCase(), "the agency host did not answer through CloudFront").toContain("cloudfront");
  expect(res!.status()).toBe(200);
  return (await res!.json().catch(() => ({}))) as Record<string, unknown>;
}

/**
 * /portal/dashboard on the agency host: the partner sign-in (password + authenticator) when the CRM web asks for it,
 * the chooser when the person has several studios (picks E2EP2TENANT), and "/portal-access/none" polled until the
 * activation lands (cfg.readyMs). Ends on the rendered dashboard.
 */
export async function openStudio(cfg: Phase2Config, page: Page, creds: PersonCredentials): Promise<{ waitedForActivationMs: number }> {
  const started = Date.now();
  let signedIn = false;
  let waited = 0;
  await page.goto(`${cfg.portal}/portal/dashboard`);
  for (let hops = 0; hops < 40; hops += 1) {
    await page.waitForLoadState("domcontentloaded");
    const u = new URL(page.url());
    if (u.origin !== cfg.portal) {
      // Managed login (partner pool): the CRM web's /auth/refresh found no session on this host.
      if (signedIn) throw new Error(`sent to sign-in again (${u.host})`);
      await signIn(page, creds);
      await skipPasskeyOffer(page);
      signedIn = true;
      await page.waitForURL((x) => x.origin === cfg.portal, { timeout: 60_000 });
      continue;
    }
    if (u.pathname === "/portal/dashboard") {
      await expect(page.getByTestId("portal-sidebar")).toBeVisible({ timeout: 30_000 });
      return { waitedForActivationMs: waited };
    }
    if (u.pathname === "/portal-access/select") {
      await page.getByTestId("studio-option").filter({ has: page.locator(`input[value="${cfg.tenant}"]`) }).getByRole("button").click();
      await page.waitForURL((x) => x.pathname !== "/portal-access/select", { timeout: 30_000 });
      continue;
    }
    if (u.pathname === "/portal-access/none") {
      if (Date.now() - started > cfg.readyMs) throw new Error("no Content studio access after the activation window (still being set up?)");
      await page.waitForTimeout(15_000);
      waited = Date.now() - started;
      await page.goto(`${cfg.portal}/portal/dashboard`);
      continue;
    }
    // An intermediate CRM web page (e.g. /auth/refresh rendering): let it move on.
    await page.waitForURL((x) => x.href !== u.href, { timeout: 30_000 });
  }
  throw new Error("too many hops on the way to the studio");
}

/** The tenant JWT the portal minted for this browser session (httpOnly cookie, read through the context). */
export async function studioToken(context: BrowserContext, portal: string): Promise<string> {
  const c = (await context.cookies(portal)).find((x) => x.name === "cis_tenant_token");
  if (!c?.value) throw new Error("no studio token cookie on the agency host");
  return c.value;
}

/** The pinned browser (one context per person); the resolver rule is on the whole browser (config). */
export async function newPinnedContext(browser: Browser): Promise<BrowserContext> {
  return browser.newContext({ viewport: { width: 1440, height: 900 } });
}
