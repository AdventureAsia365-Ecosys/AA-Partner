// e2e/smoke-portal/portal-smoke.spec.ts
// AA-885 — the tenant portal page smoke (moved out of tests/e2e/smoke/ui-smoke.spec.ts when the
// portal became its own app). For each page, logged in as the test tenant (storage state from
// auth.setup.ts), the shared runPageCheck asserts: OK navigation, no failing same-origin /api/ call,
// no console error, not blank. The portal adds a 390px no-sideways-scroll check and screenshots at
// 1440 and 390, light and dark (AA-759: the portal follows the theme now).
//
// "Not blank": pages built on the kit show a DataTable row or the kit empty state; the others pass
// on their page title (`main :is(h1, h2)`; every portal page renders one). Data errors on those pages
// still fail through the /api/ and console checks.
import { test, expect } from '@playwright/test';
import { baseUrl, installBypassRoute, runPageCheck, type PageSpec } from './smoke-helpers';
import {
  NO_LOGIN_REASON,
  PORTAL_RESULTS_DIR,
  TENANT_STATE,
  assertPortalNoOverflowAt390,
  ensurePortalResultsDir,
  hasTenantState,
  portalScreenshots,
  waitForPortalData,
} from './portal-helpers';

ensurePortalResultsDir();

// Page title: PageHeader renders an h1; the older tabs (Brand Identity) an h2.
const RENDERED = 'main :is(h1, h2)';

// Add a page = add one line here (the portal nav + the deep-link-only pages). AA-759: new paths, no
// T-number slugs (the old ones answer 308 — aa885-portal-host.spec.ts checks each).
const PORTAL_PAGES: PageSpec[] = [
  { id: 'dashboard', label: 'Dashboard', path: '/portal/dashboard', readySelector: RENDERED },
  // AA-671 — one Marketplace page: the catalog, and the tenant's picked tours as the view ?view=mine.
  { id: 'tours', label: 'Marketplace', path: '/portal/tours', readySelector: RENDERED },
  { id: 'tours-mine', label: 'My Tours', path: '/portal/tours/mine', readySelector: RENDERED },
  { id: 'tours-mine-view', label: 'Marketplace, In my catalog', path: '/portal/tours?view=mine', readySelector: RENDERED },
  { id: 'brand', label: 'Brand Identity', path: '/portal/brand', readySelector: RENDERED },
  // AA-814 — Brand › Facts (CIS /v1/facts: the agency's own facts and the Adventure Asia facts it can replace).
  { id: 'facts', label: 'Facts', path: '/portal/facts', readySelector: RENDERED },
  { id: 'create-social', label: 'Social Posts', path: '/portal/create/social', readySelector: RENDERED },
  { id: 'create-article', label: 'Articles', path: '/portal/create/article', readySelector: RENDERED },
  { id: 'content', label: 'My Content', path: '/portal/content', readySelector: RENDERED },
  { id: 'content-publish', label: 'Publish', path: '/portal/content/publish', readySelector: RENDERED },
  { id: 'account-billing', label: 'Billing', path: '/portal/account/billing', readySelector: RENDERED },
  { id: 'account-activity', label: 'Activity', path: '/portal/account/activity', readySelector: RENDERED },
  { id: 'account-settings', label: 'Account settings', path: '/portal/account/settings', readySelector: RENDERED },
  { id: 'account-members', label: 'Members', path: '/portal/account/members', readySelector: RENDERED },
  { id: 'account-api', label: 'API', path: '/portal/account/api', readySelector: RENDERED },
  { id: 'account-integrations', label: 'Integrations', path: '/portal/account/integrations', readySelector: RENDERED },
  { id: 'help-facebook', label: 'Connect your Facebook Page', path: '/portal/help/facebook', readySelector: RENDERED },
];

test.describe('UI smoke — portal pages', () => {
  test.skip(() => !hasTenantState(), NO_LOGIN_REASON);

  for (const spec of PORTAL_PAGES) {
    test(`portal: ${spec.label} (${spec.path})`, async ({ browser }) => {
      await runPageCheck(browser, TENANT_STATE, spec, {
        afterChecks: (page, s) => assertPortalNoOverflowAt390(page, s.label),
        screenshots: portalScreenshots,
      });
    });
  }

  // AA-935 (CRM-07, design v3 Fix C10) — the sidebar links to the Partner CRM (another app, its own sign-in until
  // AA-979), and the CRM sidebar links back to the Content studio. Only the link is checked here: the CRM host has
  // its own smoke in AA-Partner.
  test('portal: sidebar links to the Partner CRM', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE, viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/portal/dashboard', { waitUntil: 'domcontentloaded' });
      const link = page.getByTestId('portal-crm-link');
      await expect(link).toBeVisible({ timeout: 15000 });
      // AA-979 §4.5: one agency host for both apps, so the link is the same-origin CRM home.
      expect(await link.getAttribute('href')).toBe('/');
    } finally {
      await context.close();
    }
  });

  // AA-671 — a Marketplace card opens the detail drawer with the tour's next step, and Escape closes it. Read only: the
  // test never presses a rewrite button. Every wait is bounded.
  test('portal: Marketplace card opens the detail drawer', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE, viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/portal/tours', { waitUntil: 'domcontentloaded' });
      await waitForPortalData(page);
      const card = page.getByTestId('marketplace-card').first();
      await expect(card, 'no tour card on the Marketplace').toBeVisible({ timeout: 15000 });
      const name = (await card.getByRole('button').first().getAttribute('aria-label'))?.replace(/^Open /, '') ?? '';
      await card.getByRole('button').first().click();
      const drawer = page.getByRole('dialog');
      await expect(drawer).toBeVisible({ timeout: 10000 });
      if (name) await expect(drawer).toContainText(name);
      await expect(page.getByTestId('marketplace-drawer-actions').getByRole('button').first()).toBeVisible({ timeout: 10000 });
      await page.keyboard.press('Escape');
      await expect(drawer).toHaveCount(0, { timeout: 10000 });
    } finally {
      await context.close();
    }
  });

  // AA-671 — the Marketplace filters by activity, intensity and "recently added", and each catalog card has a cover
  // area (the tour's photo, or the placeholder when it has none). Read only; every wait is bounded.
  test('portal: Marketplace activity / intensity / recently added filters and card covers', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE, viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/portal/tours', { waitUntil: 'domcontentloaded' });
      await waitForPortalData(page);
      const card = page.getByTestId('marketplace-card').first();
      await expect(card, 'no tour card on the Marketplace').toBeVisible({ timeout: 15000 });
      await expect(card.getByTestId('marketplace-card-cover')).toHaveAttribute('data-cover', /^(photo|none)$/);
      // A photo cover is responsive and lazy: both CIS renditions in srcset (600 / 1600 px), loading="lazy".
      const photoImg = page.locator('[data-testid="marketplace-card-cover"][data-cover="photo"] img').first();
      if (await photoImg.count()) {
        await expect(photoImg).toHaveAttribute('srcset', /600w, .+ 1600w$/);
        await expect(photoImg).toHaveAttribute('loading', 'lazy');
      }
      const filters = page.getByTestId('marketplace-filters');
      for (const label of ['Activity', 'Intensity', 'Recently added']) {
        await expect(filters.getByLabel(label, { exact: true }), `no ${label} filter`).toBeVisible({ timeout: 10000 });
      }
      // A filter goes to the API and the page answers with a count or the empty state, never the error state.
      const pool = page.waitForResponse((r) => r.url().includes('/v1/tours/pool') && r.url().includes('added_within_days=90'),
        { timeout: 15000 });
      await filters.getByLabel('Recently added', { exact: true }).selectOption('90');
      expect((await pool).status()).toBe(200);
      await expect(page.getByTestId('marketplace-count').or(page.getByText('No tours found'))).toBeVisible({ timeout: 15000 });
    } finally {
      await context.close();
    }
  });

  // AA-760 — the Marketplace (was Browse Tours) "no results" state (a search that matches no tour), captured in light
  // and dark at 1440 and 390 like the page screenshots. Every wait is bounded.
  test('portal: Marketplace shows its empty state for a search with no match', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE, viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      ensurePortalResultsDir();
      for (const scheme of ['light', 'dark'] as const) {
        // The search is page state: each scheme loads the page and searches again.
        await page.emulateMedia({ colorScheme: scheme });
        await page.goto('/portal/tours', { waitUntil: 'domcontentloaded' });
        await waitForPortalData(page);
        await page.getByPlaceholder('Search tours by name…').fill('zzqx-no-such-tour-aa760');
        await expect(page.getByText('No tours found')).toBeVisible({ timeout: 15000 });
        for (const vp of [{ name: 'desktop', width: 1440, height: 900 }, { name: 'mobile', width: 390, height: 844 }]) {
          await page.setViewportSize({ width: vp.width, height: vp.height });
          await page.waitForTimeout(300);
          await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/tours-no-results-${vp.name}-${scheme}.png` });
        }
      }
    } finally {
      await context.close();
    }
  });

  // AA-831 (S239 UI verify D) — at 390px the longest portal breadcrumb ("Connect your Facebook
  // Page") was hard-clipped next to the bell. The kit Breadcrumb must end the title in an ellipsis
  // and stay inside the top bar, with no page-level horizontal overflow. Every wait is bounded.
  test('portal: top-bar breadcrumb truncates with an ellipsis at 390px', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: TENANT_STATE,
      viewport: { width: 390, height: 844 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/portal/help/facebook', { waitUntil: 'domcontentloaded' });
      const crumb = page.getByTestId('topbar-breadcrumb');
      const title = page.getByTestId('topbar-breadcrumb-title');
      await expect(title).toHaveText('Connect your Facebook Page', { timeout: 15000 });
      // AA-759 — the shell's phone layout is CSS now (no hydration switch); waiting for the
      // hamburger still guards against measuring before the styles applied.
      await expect(page.getByRole('button', { name: 'Open menu' })).toBeVisible({ timeout: 15000 });

      const m = await page.evaluate(() => {
        const t = document.querySelector<HTMLElement>('[data-testid="topbar-breadcrumb-title"]')!;
        const c = document.querySelector<HTMLElement>('[data-testid="topbar-breadcrumb"]')!;
        const header = c.closest('header')!;
        const cs = getComputedStyle(t);
        const cr = c.getBoundingClientRect();
        const hr = header.getBoundingClientRect();
        // The first header control after the crumb (search box or bell) must not be overlapped.
        const after = Array.from(header.querySelectorAll<HTMLElement>('button'))
          .map((b) => b.getBoundingClientRect())
          .filter((r) => r.width > 0 && r.left >= cr.left + 1)
          .map((r) => r.left);
        return {
          textOverflow: cs.textOverflow,
          whiteSpace: cs.whiteSpace,
          overflowX: cs.overflowX,
          titleScroll: t.scrollWidth,
          titleClient: t.clientWidth,
          crumbRight: Math.round(cr.right),
          headerRight: Math.round(hr.right),
          nextControlLeft: after.length ? Math.round(Math.min(...after)) : null,
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
        };
      });

      await expect(crumb).toBeVisible();
      expect(m.textOverflow, 'breadcrumb title must use text-overflow: ellipsis').toBe('ellipsis');
      expect(m.whiteSpace).toBe('nowrap');
      expect(m.overflowX).toBe('hidden');
      // A crumb squeezed to almost nothing would satisfy every bound below; the title must keep
      // usable width (at 390px the mobile bar leaves roughly 180px for it).
      expect(m.titleClient, `breadcrumb title has no usable width (clientWidth ${m.titleClient})`).toBeGreaterThan(40);
      expect(m.crumbRight, 'breadcrumb must stay inside the top bar').toBeLessThanOrEqual(m.headerRight);
      if (m.nextControlLeft !== null) {
        expect(m.crumbRight, 'breadcrumb must not run under the next top-bar control').toBeLessThanOrEqual(m.nextControlLeft);
      }
      expect(
        m.scrollWidth,
        `page-level horizontal overflow at 390px (scrollWidth ${m.scrollWidth} > innerWidth ${m.innerWidth})`,
      ).toBeLessThanOrEqual(m.innerWidth + 1);
      // Informational: at 390px this title is expected to be truncated (scrollWidth > clientWidth).
      test.info().annotations.push({
        type: 'breadcrumb',
        description: `title scrollWidth=${m.titleScroll} clientWidth=${m.titleClient} truncated=${m.titleScroll > m.titleClient}`,
      });
    } finally {
      await context.close();
    }
  });
});
