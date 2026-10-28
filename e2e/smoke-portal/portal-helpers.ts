// e2e/smoke-portal/portal-helpers.ts
// AA-885 — helpers for the tenant portal smoke (project `smoke-portal`, BASE_URL = a portal host:
// https://portal.lumiguides.it.com, or http://localhost:3004). The generic page
// check, bypass route and watcher come from ./smoke-helpers.ts.
import { expect, type Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { TENANT_STATE } from './smoke-helpers';

export const PORTAL_RESULTS_DIR = 'results/smoke-portal';

/**
 * AA-979 P2b — which studio the smoke checks: `fargate` (the agency host portal.: the studio behind CloudFront -> ALB,
 * sharing the host with the CRM web; PORTAL_TARGET=fargate, what CI always sets since the P2b clean-up) or a local
 * studio without the partner sign-in (default; started with PORTAL_API_KEY_SIGNIN=true). On the agency host a
 * signed-out page goes to the CRM web's /auth/refresh (partner sign-in), "/" is the CRM home, and the paths the studio
 * does not own belong to the CRM web.
 */
export const FARGATE = process.env.PORTAL_TARGET === 'fargate';
/** Where a signed-out /portal/* page is sent. */
export const SIGNED_OUT_PATH = FARGATE ? '/auth/refresh' : '/tenant-login';

export { TENANT_STATE };

/** The e2e studio user's partner-pool sign-in (CI secrets E2EPORTALEMAIL / E2EPORTALPASSWORD / E2EPORTALTOTP). */
export function partnerCredentials(): { email: string; password: string; totpSecret: string } | null {
  const email = (process.env.E2EPORTALEMAIL ?? '').trim();
  const password = process.env.E2EPORTALPASSWORD ?? '';
  const totpSecret = (process.env.E2EPORTALTOTP ?? '').trim();
  return email && password && totpSecret ? { email, password, totpSecret } : null;
}

/**
 * True when the setup can sign a tenant in: on the agency host with the studio user's partner sign-in (AA-979 §4.5:
 * the API-key sign-in is off there); on a local studio with the tenant test key (E2E_TENANT_API_KEY).
 */
export function hasTenantLogin(): boolean {
  return FARGATE ? partnerCredentials() !== null : Boolean(process.env.E2E_TENANT_API_KEY);
}

/** True when the setup project wrote a tenant storage state in this run. */
export function hasTenantState(): boolean {
  return hasTenantLogin() && fs.existsSync(TENANT_STATE);
}

export const NO_LOGIN_REASON = FARGATE
  ? 'No studio sign-in: set E2EPORTALEMAIL, E2EPORTALPASSWORD and E2EPORTALTOTP (the e2e studio user) to run the logged-in portal smoke.'
  : 'No tenant test key: set E2E_TENANT_API_KEY (a local studio with PORTAL_API_KEY_SIGNIN=true) to run the logged-in portal smoke.';

export function ensurePortalResultsDir(): void {
  fs.mkdirSync(PORTAL_RESULTS_DIR, { recursive: true });
}

/**
 * AA-885 / AA-605 — at 390px the portal must not scroll sideways (the sidebar is an off-canvas
 * drawer below 900px). Waits for the shell's mobile layout (the hamburger; CSS-driven since AA-759),
 * measures `documentElement.scrollWidth <= innerWidth` and lists the elements that stick out when it
 * fails (lesson S221: measure the offender, do not guess). Restores the desktop viewport after.
 */
export async function assertPortalNoOverflowAt390(page: Page, label: string): Promise<void> {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'Open menu' }), `${label}: portal shell did not switch to its phone layout`)
    .toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(250);
  const m = await page.evaluate(() => {
    const innerWidth = window.innerWidth;
    const scrollable = (el: HTMLElement): boolean => {
      let node: HTMLElement | null = el.parentElement;
      while (node && node !== document.documentElement) {
        const ox = getComputedStyle(node).overflowX;
        if (ox === 'auto' || ox === 'hidden' || ox === 'scroll') return true;
        node = node.parentElement;
      }
      return false;
    };
    const offenders: string[] = [];
    for (const el of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
      const r = el.getBoundingClientRect();
      if (r.right <= innerWidth + 1 || r.width === 0 || r.height === 0 || scrollable(el)) continue;
      offenders.push(`<${el.tagName.toLowerCase()} data-testid="${el.getAttribute('data-testid') ?? ''}"> right=${Math.round(r.right)}`);
    }
    return { scrollWidth: document.documentElement.scrollWidth, innerWidth, offenders: offenders.slice(0, 12) };
  });
  expect(
    m.scrollWidth,
    `${label}: page-level horizontal overflow at 390px (scrollWidth ${m.scrollWidth} > innerWidth ${m.innerWidth}). Offenders:\n    ${m.offenders.join('\n    ') || '(none found)'}`,
  ).toBeLessThanOrEqual(m.innerWidth + 1);
  await page.setViewportSize({ width: 1440, height: 900 });
}

/**
 * AA-760 — wait until the page shows data, not its loading state: the network goes quiet (bounded:
 * a page that polls never goes idle) and no loading placeholder is left (`aria-busy="true"` marks
 * every portal skeleton / spinner). The S247 screenshot audit found every desktop-dark capture still
 * on a skeleton (the dark pass reloads, then waited only 300 ms).
 */
export async function waitForPortalData(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await page
    .waitForFunction(() => !document.querySelector('[aria-busy="true"]'), null, { timeout: 15000 })
    .catch(() => {});
}

/**
 * AA-760 — the portal scrolls inside `<main data-pshell="main">`, not the document, so Playwright's
 * fullPage stopped at 900 px. For the desktop capture the viewport is made as tall as main's content
 * (capped), so the whole page is in the shot.
 */
export async function fitViewportToMain(page: Page, width: number, minHeight: number): Promise<void> {
  const contentHeight = await page.evaluate(() => {
    const main = document.querySelector<HTMLElement>('[data-pshell="main"]');
    if (!main) return 0;
    return main.getBoundingClientRect().top + main.scrollHeight;
  });
  await page.setViewportSize({ width, height: Math.min(Math.max(minHeight, Math.ceil(contentHeight)), 8000) });
}

/** Portal screenshots: desktop 1440 (the whole page) + phone 390 (first screen), in light and dark.
 * AA-759: the portal follows the theme; with no stored choice it follows `prefers-color-scheme`,
 * so emulating the colour scheme is enough (the page reloads so the pre-paint restore script runs).
 * AA-760: each capture waits for the data (waitForPortalData), and the desktop one covers the full
 * height of the main scroll container. */
export async function portalScreenshots(page: Page, slug: string): Promise<void> {
  ensurePortalResultsDir();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    if (scheme === 'dark') await page.reload({ waitUntil: 'domcontentloaded' });
    await waitForPortalData(page);
    for (const vp of [
      { name: 'desktop', width: 1440, height: 900 },
      { name: 'mobile', width: 390, height: 844 },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(300);
      if (vp.name === 'desktop') await fitViewportToMain(page, vp.width, vp.height);
      await page.screenshot({ path: path.join(PORTAL_RESULTS_DIR, `${slug}-${vp.name}-${scheme}.png`) });
    }
  }
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
}
