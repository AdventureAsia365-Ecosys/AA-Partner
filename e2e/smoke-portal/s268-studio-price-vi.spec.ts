// e2e/smoke-portal/s268-studio-price-vi.spec.ts
// S268 — two studio checks on the agency host, logged in as the E2E studio tenant (storage state from auth.setup.ts).
// Read only: nothing here writes, rewrites or meters; the language choice is a cookie of this browser context only.
//
//   - AA-1029 (from price): the Marketplace (/portal/tours) searched for one live Dev tour whose AA-Booking
//     price.from.v1 row is 235 USD per person for 2. The card and the tour drawer both show "From $235 per person" and
//     the caveat line; screenshots of the card and the drawer at 1440 and 390, light and dark. Dev data drift fails
//     fast with a message naming the tour and what the pool API answered (no row, a null price, another price), not
//     with a locator timeout.
//   - AA-1036 (Vietnamese): the language is switched through the account menu's "Language and dates" (the CRM's
//     LocaleSwitch: POST /prefs/locale, cookie aa_crm_locale, the page reloads). Dashboard, Marketplace and Brand
//     Identity are checked by one heading each from the studio *.messages.ts catalogues and captured (desktop light,
//     phone dark), then the language is switched back and an English heading is checked.
import { test, expect, type Browser, type BrowserContext, type Page } from '@playwright/test';
import { baseUrl, describeApiFailures, installBypassRoute, watchPage, type PageWatcher } from './smoke-helpers';
import {
  NO_LOGIN_REASON,
  PORTAL_RESULTS_DIR,
  TENANT_STATE,
  ensurePortalResultsDir,
  fitViewportToMain,
  hasTenantState,
  waitForPortalData,
} from './portal-helpers';

ensurePortalResultsDir();

const DESKTOP = { name: 'desktop', width: 1440, height: 900 } as const;
const MOBILE = { name: 'mobile', width: 390, height: 844 } as const;

async function newStudioContext(browser: Browser, colorScheme: 'light' | 'dark'): Promise<BrowserContext> {
  const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE, colorScheme,
    viewport: { width: DESKTOP.width, height: DESKTOP.height } });
  await installBypassRoute(context);
  return context;
}

async function open(page: Page, path: string): Promise<void> {
  const resp = await page.goto(path, { waitUntil: 'domcontentloaded' });
  expect(resp?.status() ?? 0, `${path}: navigation failed`).toBeLessThan(400);
  await waitForPortalData(page);
}

async function assertClean(watcher: PageWatcher): Promise<void> {
  await watcher.settle();
  expect(watcher.apiFailures, describeApiFailures(watcher.apiFailures)).toEqual([]);
  expect(watcher.consoleErrors.map((e) => e.text)).toEqual([]);
}

// ── AA-1029: the from price ────────────────────────────────────────────────────────────────────────

/** The live Dev tour with a Booking price.from.v1 row, and what that row says. */
const PRICED_TOUR = 'Discover Okinawa';
const EXPECTED_PRICE = { amount: 235, currency: 'USD', pax: 2 };
/** FromPriceLine's text for that row (TourCard.tsx; marketplace.messages.ts + lib.messages.ts priceNote, English). */
const PRICE_LINE = 'From $235 per person';
const PRICE_NOTE = 'Suggested price for 2 travellers. Your advisor confirms the final price.';

type PoolRow = {
  id: string;
  aa_name: string | null;
  from_price?: { amount: string; currency: string; pax: number } | null;
};

/** Opens the Marketplace, searches the priced tour and returns its pool row once its price is known to be the
 *  expected one; every data problem is a failure that names the tour and what the API answered. */
async function searchPricedTour(page: Page): Promise<PoolRow> {
  await open(page, '/portal/tours');
  const search = page.getByTestId('marketplace-filters').getByLabel('Search tours by name');
  await expect(search, 'the Marketplace search box did not render').toBeVisible({ timeout: 15000 });
  const answer = page.waitForResponse((r) => {
    const u = new URL(r.url());
    return u.pathname === '/api/tenant/v1/tours/pool' && u.searchParams.get('search') === PRICED_TOUR;
  }, { timeout: 20000 });
  await search.fill(PRICED_TOUR);
  const resp = await answer;
  expect(resp.status(), `GET /v1/tours/pool?search=${PRICED_TOUR}`).toBe(200);
  const rows = ((await resp.json()) as { data?: PoolRow[] }).data ?? [];
  const row = rows.find((r) => (r.aa_name ?? '').toLowerCase().includes(PRICED_TOUR.toLowerCase()));
  expect(row, `Dev data drift: the pool search for "${PRICED_TOUR}" has no such tour `
    + `(${rows.length} rows: ${rows.map((r) => r.aa_name).join(', ') || 'none'})`).toBeDefined();
  const price = row!.from_price;
  const answered = row && 'from_price' in row ? JSON.stringify(price) : 'no from_price field (an older CIS pool answer)';
  expect(price ?? null, `Dev data drift: the pool API answers "${row!.aa_name}" (${row!.id}) with from_price `
    + `${answered}: no Booking price.from.v1 price reaches the studio (expected ${EXPECTED_PRICE.amount} `
    + `${EXPECTED_PRICE.currency} per person for ${EXPECTED_PRICE.pax})`).not.toBeNull();
  expect({ amount: Number(price!.amount), currency: price!.currency, pax: price!.pax },
    `Dev data drift: "${row!.aa_name}" (${row!.id}) has another price.from.v1 row than this check expects`)
    .toEqual(EXPECTED_PRICE);
  await waitForPortalData(page);
  return row!;
}

/** The card's price block and the drawer's price line both say "From $235 per person" plus the caveat; captures the
 *  card and the drawer at the current viewport and scheme. Leaves the drawer closed. */
async function checkCardAndDrawer(page: Page, row: PoolRow, vp: typeof DESKTOP | typeof MOBILE, scheme: string) {
  const name = row.aa_name ?? '';
  const card = page.getByTestId('marketplace-card')
    .filter({ has: page.getByRole('button', { name: `Open ${name}`, exact: true }) }).first();
  await expect(card, `${vp.name}: no Marketplace card for "${name}"`).toBeVisible({ timeout: 15000 });
  const cardPrice = card.getByTestId('marketplace-from-price');
  await expect(cardPrice, `${vp.name}: the "${name}" card shows no from price`).toBeVisible({ timeout: 10000 });
  await expect(cardPrice).toContainText(PRICE_LINE);
  await expect(cardPrice).toContainText(PRICE_NOTE);
  await waitForPortalData(page);
  await card.scrollIntoViewIfNeeded();
  await card.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa1029-from-price-card-${vp.name}-${scheme}.png` });

  await card.getByRole('button', { name: `Open ${name}`, exact: true }).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer, `${vp.name}: the tour drawer did not open`).toBeVisible({ timeout: 10000 });
  await expect(drawer).toContainText(name);
  const drawerPrice = drawer.getByTestId('marketplace-drawer-price');
  await expect(drawerPrice, `${vp.name}: the "${name}" drawer shows no from price`).toBeVisible({ timeout: 10000 });
  await expect(drawerPrice).toContainText(PRICE_LINE);
  await expect(drawerPrice).toContainText(PRICE_NOTE);
  await waitForPortalData(page);
  await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa1029-from-price-drawer-${vp.name}-${scheme}.png` });
  await page.keyboard.press('Escape');
  await expect(drawer).toHaveCount(0, { timeout: 10000 });
}

// ── AA-1036: Vietnamese ────────────────────────────────────────────────────────────────────────────

/** One heading per page, from the studio catalogues: dashboard.messages.ts (welcome / title), content.messages.ts
 *  (marketTitle), brand.messages.ts BRAND_MESSAGES (title). */
const VI_PAGES = [
  { slug: 'dashboard', path: '/portal/dashboard', level: 1, vi: /^(Chào mừng trở lại, .+|Tổng quan)$/ },
  { slug: 'tours', path: '/portal/tours', level: 1, vi: /^Kho tour$/ },
  { slug: 'brand', path: '/portal/brand', level: 2, vi: /^Nhận diện thương hiệu$/ },
] as const;

/** Opens the account menu and picks a locale in "Language and dates"; waits for the reload in that language. */
async function chooseLocale(page: Page, locale: string): Promise<void> {
  await page.getByRole('button', { name: /^(Account menu|Menu tài khoản)$/ }).click();
  const select = page.getByTestId('portal-locale').getByTestId('locale-select');
  await expect(select, 'the account menu has no language switch').toBeVisible({ timeout: 10000 });
  const posted = page.waitForRequest((r) => new URL(r.url()).pathname === '/prefs/locale' && r.method() === 'POST',
    { timeout: 15000 });
  await select.selectOption(locale);
  await posted;
  await expect(page.locator('html'), `the page did not reload in ${locale}`)
    .toHaveAttribute('lang', locale === 'vi-VN' ? 'vi' : 'en', { timeout: 20000 });
  await waitForPortalData(page);
}

/** The locale the person has now (the switch's selected option); opens and closes the account menu. */
async function currentLocale(page: Page): Promise<string> {
  const menu = page.getByRole('button', { name: /^(Account menu|Menu tài khoản)$/ });
  await menu.click();
  const select = page.getByTestId('portal-locale').getByTestId('locale-select');
  await expect(select, 'the account menu has no language switch').toBeVisible({ timeout: 10000 });
  const value = await select.inputValue();
  await page.keyboard.press('Escape');
  await expect(select).toHaveCount(0, { timeout: 5000 });
  return value;
}

test.describe('UI smoke — studio from price and Vietnamese (S268)', () => {
  test.skip(() => !hasTenantState(), NO_LOGIN_REASON);

  test('Marketplace: the priced tour shows "From $235 per person" on the card and in the drawer', async ({ browser }) => {
    test.setTimeout(180_000);
    for (const scheme of ['light', 'dark'] as const) {
      const context = await newStudioContext(browser, scheme);
      const page = await context.newPage();
      const watcher = watchPage(page);
      try {
        const row = await searchPricedTour(page);
        for (const vp of [DESKTOP, MOBILE]) {
          await page.setViewportSize({ width: vp.width, height: vp.height });
          await page.waitForTimeout(300);
          await checkCardAndDrawer(page, row, vp, scheme);
        }
        await assertClean(watcher);
      } finally {
        await context.close();
      }
    }
  });

  test('Vietnamese through "Language and dates": dashboard, Marketplace, Brand Identity, then back to English', async ({ browser }) => {
    test.setTimeout(240_000);
    const context = await newStudioContext(browser, 'light');
    const page = await context.newPage();
    const watcher = watchPage(page);
    try {
      await open(page, '/portal/dashboard');
      await expect(page.locator('html')).toHaveAttribute('lang', 'en', { timeout: 15000 });
      const start = await currentLocale(page);
      // Back to the person's English variant afterwards (English by default: en-AU, lib/i18n.ts DEFAULT_LOCALE).
      const english = start.startsWith('en-') ? start : 'en-AU';

      await chooseLocale(page, 'vi-VN');
      for (const p of VI_PAGES) {
        await open(page, p.path);
        const heading = page.locator('main').getByRole('heading', { level: p.level, name: p.vi });
        await expect(heading.first(), `${p.path}: no Vietnamese heading ${p.vi}`).toBeVisible({ timeout: 15000 });
        await waitForPortalData(page);
        await fitViewportToMain(page, DESKTOP.width, DESKTOP.height);
        await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa1036-vi-${p.slug}-desktop-light.png` });

        // Phone, dark: the theme's pre-paint script reads prefers-color-scheme, so the page reloads after the switch.
        await page.emulateMedia({ colorScheme: 'dark' });
        await page.setViewportSize({ width: MOBILE.width, height: MOBILE.height });
        await page.reload({ waitUntil: 'domcontentloaded' });
        await waitForPortalData(page);
        await expect(heading.first(), `${p.path} (390, dark): no Vietnamese heading ${p.vi}`).toBeVisible({ timeout: 15000 });
        await page.waitForTimeout(300);
        await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa1036-vi-${p.slug}-mobile-dark.png` });
        await page.emulateMedia({ colorScheme: 'light' });
        await page.setViewportSize({ width: DESKTOP.width, height: DESKTOP.height });
      }

      // Back to English, checked on the page we are on (Brand Identity, brand.messages.ts BRAND_MESSAGES title).
      await chooseLocale(page, english);
      await expect(page.locator('main').getByRole('heading', { level: 2, name: 'Brand Identity', exact: true }),
        'the studio did not come back in English').toBeVisible({ timeout: 15000 });
      expect(await currentLocale(page)).toBe(english);
      await assertClean(watcher);
    } finally {
      await context.close();
    }
  });
});
