// e2e/smoke-portal/auth.setup.ts
// AA-885 — `portal-setup` project: a tenant signs in ONCE and the storage state is written to
// e2e/.auth/tenant-state.json (never committed, never uploaded; the workflow deletes it). Skips (not fails) without
// the sign-in values, so the unauthenticated host checks still run.
//
// AA-979 §4.5 (P2b clean-up): on the agency host (PORTAL_TARGET=fargate) the API-key sign-in is off, so the setup signs
// in like a person: /portal/dashboard -> the CRM web's /auth/refresh -> partner-pool managed login (password + the
// authenticator code) -> back on the agency host, where the studio exchanges the partner session for its own token.
// The user is the e2e studio user (E2EPORTALEMAIL / E2EPORTALPASSWORD / E2EPORTALTOTP): a synthetic partner identity
// with a Content studio membership on the E2E tenant. A local studio without the partner sign-in still uses the
// tenant test key on /tenant-login (PORTAL_API_KEY_SIGNIN=true there).
import { expect, test as setup, type Page } from '@playwright/test';
import { signIn, skipPasskeyOffer } from '../helpers/signin';
import { baseUrl, ensureAuthDir } from './smoke-helpers';
import { FARGATE, NO_LOGIN_REASON, TENANT_STATE, hasTenantLogin, partnerCredentials } from './portal-helpers';

/** Partner sign-in on the agency host; ends on the rendered studio dashboard. */
async function partnerSignIn(page: Page): Promise<void> {
  const creds = partnerCredentials()!;
  const origin = new URL(baseUrl()).origin;
  let signedIn = false;
  await page.goto('/portal/dashboard', { waitUntil: 'domcontentloaded' });
  for (let hops = 0; hops < 30; hops += 1) {
    await page.waitForLoadState('domcontentloaded');
    const u = new URL(page.url());
    if (u.origin !== origin) {
      // Managed login of the partner pool: the CRM web's /auth/refresh found no session on this host.
      if (signedIn) throw new Error(`sent to the sign-in again (${u.host}): check the e2e studio user's password and authenticator`);
      await signIn(page, creds);
      await skipPasskeyOffer(page);
      signedIn = true;
      await page.waitForURL((x) => x.origin === origin, { timeout: 60_000 });
      continue;
    }
    if (u.pathname.startsWith('/portal/')) {
      await expect(page.getByTestId('portal-sidebar')).toBeVisible({ timeout: 30_000 });
      return;
    }
    if (u.pathname === '/portal-access/select') {
      // Several studios: the first one (the e2e user is meant to have one, on the E2E tenant).
      await page.getByTestId('studio-option').first().getByRole('button').click();
      await page.waitForURL((x) => x.pathname !== '/portal-access/select', { timeout: 30_000 });
      continue;
    }
    if (u.pathname === '/portal-access/none') {
      throw new Error('the e2e studio user has no active Content studio membership (invite it on the E2E tenant)');
    }
    if (u.pathname === '/select-agency') {
      // The CRM web asks which agency when the person belongs to several (plain form POST).
      await page.getByTestId('agency-option').first().getByRole('button').click();
      await page.waitForURL((x) => x.pathname !== '/select-agency', { timeout: 30_000 });
      continue;
    }
    if (signedIn && !u.pathname.startsWith('/auth/')) {
      // Signed in but left on a CRM web page (its home after the agency choice): go to the studio.
      await page.goto('/portal/dashboard', { waitUntil: 'domcontentloaded' });
      continue;
    }
    // An intermediate CRM web page (/auth/refresh, /auth/callback): let it move on.
    await page.waitForURL((x) => x.href !== u.href, { timeout: 30_000 });
  }
  throw new Error('too many hops on the way to the studio');
}

/** A local studio with PORTAL_API_KEY_SIGNIN=true: the API-key form. */
async function apiKeySignIn(page: Page): Promise<void> {
  await page.goto('/tenant-login', { waitUntil: 'load' });
  // AA-760 — a value typed before React hydrates the controlled input is reset to "": fill until it sticks.
  const input = page.locator('input[type="password"]');
  await expect(async () => {
    await input.fill(process.env.E2E_TENANT_API_KEY!);
    await page.waitForTimeout(250);
    await expect(input).not.toHaveValue('');
  }).toPass({ timeout: 15000 });
  await page.click('button:has-text("Access Portal")');
  await page.waitForURL(/\/portal\//, { timeout: 20000 });
}

setup('authenticate tenant', async ({ browser }) => {
  setup.skip(!hasTenantLogin(), NO_LOGIN_REASON);
  setup.setTimeout(180_000); // managed login + a possible wait for a fresh authenticator code (≤ 30 s)
  ensureAuthDir();
  const context = await browser.newContext({ baseURL: baseUrl() });
  const page = await context.newPage();
  try {
    if (FARGATE) await partnerSignIn(page);
    else await apiKeySignIn(page);
    await context.storageState({ path: TENANT_STATE });
  } finally {
    await context.close();
  }
});
