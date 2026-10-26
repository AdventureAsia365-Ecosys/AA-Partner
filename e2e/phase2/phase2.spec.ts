// e2e/phase2/phase2.spec.ts — AA-979 P2b phase 2 (root ADR 0010a; runbook §2.4-§2.7, §3, §6a): the browser steps of
// the Content studio re-enrolment and the studio on the agency host, run by the orchestrator on demand only:
//
//   (a) staff: a Content studio invitation for a synthetic tenant (CIS POST /admin/tenants/<id>/members/invitations
//       through the admin BFF; the CIS relays the staff member's own Cognito token to crm.ops.v1, the CRM sends the
//       AA system mail). E2EP2STAFF: "none" (the orchestrator made the call; the spec only reads mail newer than
//       E2EP2SINCE), "browser" (headed: a person signs in to the CIS admin, the spec makes the call in that session),
//       "a10b" (the Dev-only E2E CIS admin; refused 403 e2e_read_only while that identity is read-only).
//   (b) the invitee: the mail from the e2e mailbox -> /join?t=... -> claim (one-time password) -> managed login (own
//       password + authenticator enrolment from the "Show secret key" text, RFC 6238 codes) -> accept.
//   (c) "Content studio access is ready" (or "being set up", then /portal-access/none polled until the activation
//       lands), then the studio on the agency host, pinned to the crm_agency distribution by the browser's host
//       resolver rule (playwright.phase2.config.ts), signed in with the partner pool: no API key (/api/tenant/me says
//       sign_in = partner).
//   (d) evidence screenshots, 1440 + 390, light + dark, into E2EP2OUT.
//   (e) runbook §6a: the studio token (viewer / editor) with the Bearer, bearer and two-space header shapes -> 403
//       studio_role_forbidden on a write that changes nothing even if the check were missing; 200 on a read.
//   + optional E check (E2EP2TENANTKEY: the same tenant through an API-key session shows the same tenant and pool count)
//   + sign-out from the studio ends the CRM session too.
//
// Never CI (no workflow sets E2EP2); retries 0 (a retry would ask for another one-time code); no trace, video or
// automatic screenshot (managed login pages carry codes and the authenticator secret).
import { expect, request as apiRequest, test, type BrowserContext, type Page } from "@playwright/test";
import { baseline, baselineBefore, waitForInviteLink } from "../helpers/mailbox";
import {
  a10bStaffSignIn,
  claimEnrolAccept,
  loadConfig,
  loadCredentials,
  newPinnedContext,
  openStudio,
  pinnedHealth,
  record,
  sameOriginFetch,
  shots,
  studioToken,
  waitForHumanStaffSignIn,
  type Phase2Config,
  type PersonCredentials,
} from "./phase2-helpers";

// A published tour id that does not exist: a write to it changes nothing even if the role check were missing (the
// route answers 404 / 422 before any charge); with the check, the middleware answers 403 before the route runs.
const PROBE_TOUR = "00000000-0000-4000-8000-0000000a0979";

test.describe.configure({ mode: "serial" });

test.describe("AA-979 P2b phase 2: re-enrolment and the studio on the agency host", () => {
  test.skip(process.env.E2EP2 !== "1", "on demand only: E2EP2=1 (see playwright.phase2.config.ts)");

  let cfg: Phase2Config;
  let creds: PersonCredentials;
  let before: Set<string> | undefined;
  let studioCtx: BrowserContext | undefined;
  let studio: Page | undefined;

  test.beforeAll(() => {
    cfg = loadConfig();
    record(cfg, {
      started_at: new Date().toISOString(),
      staff_mode: cfg.staff,
      role: cfg.role,
      tenant: cfg.tenant,
      invitee: cfg.invitee,
      pinned_to_distribution: Boolean(cfg.cf),
      reuse: cfg.reuse,
    });
  });

  test.afterAll(async () => {
    await studioCtx?.close();
  });

  test("(a) staff sends the Content studio invitation", async ({ browser }) => {
    test.skip(cfg.reuse, "E2EP2REUSE=1: the person enrolled in an earlier run");
    if (cfg.staff === "none") {
      before = await baselineBefore(cfg.since - 5_000);
      test.info().annotations.push({ type: "note", description: "the orchestrator made the invitation call (E2EP2STAFF=none)" });
      return;
    }
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    try {
      const page = await ctx.newPage();
      if (cfg.staff === "browser") await waitForHumanStaffSignIn(page, cfg.admin);
      else await a10bStaffSignIn(page, cfg.admin, cfg.a10bEmail);
      before = await baseline();
      const res = await sameOriginFetch(page, "POST", `/api/admin/tenants/${cfg.tenant}/members/invitations`, {
        email: cfg.invitee,
        content_studio_role: cfg.role,
        locale: "en",
      });
      if (res.status === 403 && res.text.includes("e2e_read_only")) {
        throw new Error(
          "the A10b E2E CIS admin is read-only (403 e2e_read_only): use E2EP2STAFF=browser or make the call yourself " +
            "(E2EP2STAFF=none), see PHASE2-AUTOMATION.md",
        );
      }
      expect(res.status, `invitation answered ${res.status}: ${res.text}`).toBe(201);
      const body = res.json as { member?: { membership_id?: string; status?: string }; delivery?: { status?: string; crm_status?: number } };
      record(cfg, {
        membership_id: body.member?.membership_id,
        membership_status: body.member?.status,
        delivery: body.delivery?.status,
        delivery_crm_status: body.delivery?.crm_status,
      });
      expect(body.delivery?.status, "the CRM did not take the invitation (send again: …/invitation/send)").toBe("sent");
    } finally {
      await ctx.close();
    }
  });

  test("(b) the invitee claims, enrols an authenticator and accepts", async ({ browser }) => {
    test.skip(cfg.reuse, "E2EP2REUSE=1: the person enrolled in an earlier run");
    if (!before) throw new Error("step (a) did not run");
    const link = await waitForInviteLink(cfg.invitee, before, 180_000);
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    try {
      const page = await ctx.newPage();
      const started = Date.now();
      const { status, joinOrigin } = await claimEnrolAccept(cfg, page, link);
      record(cfg, { join_host: new URL(joinOrigin).host, join_studio_status: status, enrol_seconds: Math.round((Date.now() - started) / 1000) });
    } finally {
      await ctx.close();
    }
  });

  test("(c, d) the studio opens on the agency host with the partner sign-in, no API key", async ({ browser }) => {
    creds = loadCredentials(cfg); // written by (b), or by an earlier run (E2EP2REUSE=1)
    studioCtx = await newPinnedContext(browser);
    studio = await studioCtx.newPage();
    if (cfg.cf) record(cfg, { portal_healthz: await pinnedHealth(cfg, studio) });
    const { waitedForActivationMs } = await openStudio(cfg, studio, creds);
    record(cfg, { waited_for_activation_seconds: Math.round(waitedForActivationMs / 1000) });

    const me = await sameOriginFetch(studio, "GET", "/api/tenant/me");
    expect(me.status).toBe(200);
    const who = me.json as { tenant_id?: string; tenant_name?: string; sign_in?: string };
    expect(who.sign_in, "the studio session must come from the partner sign-in, not an API key").toBe("partner");
    expect(who.tenant_id).toBe(cfg.tenant);
    record(cfg, { studio_tenant_name: who.tenant_name, studio_sign_in: who.sign_in });

    await shots(cfg, studio, "p2-03-studio-dashboard");
    await studio.goto(`${cfg.portal}/portal/content`);
    await expect(studio.getByTestId("portal-sidebar")).toBeVisible({ timeout: 30_000 });
    await shots(cfg, studio, "p2-04-studio-content");
    await studio.goto(`${cfg.portal}/portal/dashboard`);
  });

  test("(e) runbook 6a: the studio role holds on every bearer header shape", async () => {
    test.skip(cfg.role === "owner", "an owner has no studio role limit (6a needs a viewer or an editor)");
    if (!studioCtx) throw new Error("step (c) did not run");
    const token = await studioToken(studioCtx, cfg.portal);
    const api = await apiRequest.newContext({ baseURL: cfg.cisApi });
    try {
      const read = await api.get("/v1/tours/pool?page_size=1", { headers: { Authorization: `Bearer ${token}` } });
      expect(read.status(), "a read with the studio token").toBe(200);
      const total = ((await read.json()) as { pagination?: { total?: number } }).pagination?.total;
      const write =
        cfg.role === "viewer"
          ? { path: `/v1/tours/pool/${PROBE_TOUR}/rewrite`, data: {} } // viewer: any write
          : { path: "/v1/account/api-key", data: {} }; // editor: account writes (no POST route exists: harmless)
      const results: Record<string, number> = {};
      for (const [shape, header] of [
        ["Bearer", `Bearer ${token}`],
        ["bearer", `bearer ${token}`],
        ["Bearer-two-spaces", `Bearer  ${token}`],
      ] as const) {
        const res = await api.post(write.path, { headers: { Authorization: header }, data: write.data });
        results[shape] = res.status();
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
        expect(res.status(), `${cfg.role} ${shape}: POST ${write.path}`).toBe(403);
        expect(body.detail, `${cfg.role} ${shape}`).toBe("studio_role_forbidden");
      }
      record(cfg, { role_check: { role: cfg.role, write: `POST ${write.path}`, results, read_status: 200, pool_total: total } });
    } finally {
      await api.dispose();
    }
  });

  test("E check: the same tenant through an API-key session (optional)", async ({ browser }) => {
    test.skip(!cfg.tenantKey, "no E2EP2TENANTKEY");
    if (!studio) throw new Error("step (c) did not run");
    const ctx = await newPinnedContext(browser);
    try {
      const page = await ctx.newPage();
      await page.goto(`${cfg.portal}/tenant-login`, { waitUntil: "load" });
      const input = page.locator('input[type="password"]');
      await expect(async () => {
        await input.fill(cfg.tenantKey);
        await page.waitForTimeout(250);
        await expect(input).not.toHaveValue("");
      }).toPass({ timeout: 15_000 });
      await page.click('button:has-text("Access Portal")');
      await page.waitForURL(/\/portal\//, { timeout: 20_000 });
      const viaKey = (await sameOriginFetch(page, "GET", "/api/tenant/me")).json as Record<string, unknown>;
      test.skip(viaKey.tenant_id !== cfg.tenant, "the API key belongs to another tenant: no comparison");
      const viaPartner = (await sameOriginFetch(studio, "GET", "/api/tenant/me")).json as Record<string, unknown>;
      const pool = async (p: Page) =>
        ((await sameOriginFetch(p, "GET", "/api/tenant/v1/tours/pool?page_size=1")).json as { pagination?: { total?: number } })
          ?.pagination?.total;
      const [keyTotal, partnerTotal] = [await pool(page), await pool(studio)];
      expect(viaKey.sign_in).toBe("api_key");
      expect(viaPartner.tenant_name).toBe(viaKey.tenant_name);
      expect(partnerTotal).toBe(keyTotal);
      record(cfg, { e_check: { tenant_name_equal: true, pool_total: keyTotal } });
      await shots(cfg, page, "p2-06-apikey-dashboard", ["light"]);
    } finally {
      await ctx.close();
    }
  });

  test("sign-out from the studio ends the CRM session too", async () => {
    if (!studio || !studioCtx) throw new Error("step (c) did not run");
    await studio.getByRole("button", { name: "Account menu" }).click();
    await studio.getByRole("menuitem", { name: /sign out/i }).click();
    await studio.waitForURL(`${cfg.portal}/signed-out`, { timeout: 30_000 });
    await shots(cfg, studio, "p2-05-signed-out", ["light"]);
    await studio.goto(`${cfg.portal}/portal/dashboard`);
    // No CRM session left on the host: /portal/dashboard -> /auth/refresh -> the partner pool's managed login.
    await studio.waitForURL((u) => u.origin !== cfg.portal, { timeout: 30_000 });
    record(cfg, { signed_out: true, finished_at: new Date().toISOString() });
  });
});

