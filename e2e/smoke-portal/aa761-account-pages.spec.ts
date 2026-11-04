// e2e/smoke-portal/aa761-account-pages.spec.ts
// AA-761 — the Account pages with real data, logged in as the test tenant (storage state from auth.setup.ts).
//
// Real API answers (read only: nothing here writes, enqueues or meters):
//   - Billing: the rewrite meter (GET /v1/quota + /v1/billing) with its reset date, and the invoices empty state;
//   - Activity: GET /v1/account/activity answers 200 and lists the partner sign-in auth.setup.ts just made (the CIS
//     session exchange writes a tenant.login row, client portal: label "Signed in", detail "Content studio"); the
//     type filter reaches the server; "Export CSV" downloads the filtered set (CSV header of the CIS route);
//   - Integrations: each channel's health badge, and no API key card (tenant API keys were removed, AA-1036).
// One mocked case: server paging (the pager asks the API for page 2), with a 60-row total answered in the browser.
import { readFileSync } from 'node:fs';
import { test, expect, type Page } from '@playwright/test';
import { baseUrl, installBypassRoute, watchPage, describeApiFailures } from './smoke-helpers';
import {
  NO_LOGIN_REASON,
  PORTAL_RESULTS_DIR,
  TENANT_STATE,
  ensurePortalResultsDir,
  hasTenantState,
  waitForPortalData,
} from './portal-helpers';

ensurePortalResultsDir();

const CSV_HEADER = 'Date (UTC),Type,Activity,By,Detail,Resource type,Resource id';
// The row the partner sign-in of auth.setup.ts leaves (CIS api/services/tenant_account.py: tenant.login is
// "Signed in", its detail is "Content studio" for client portal). The API-key sign-in row ("Signed in with your
// API key", detail "API") went with the tenant API keys (AA-1036): a sign-in now always comes from the studio.
const SIGN_IN_LABEL = 'Signed in';
const SIGN_IN_DETAIL = 'Content studio';
// One CSV line of that row: Date,Type,Activity,By,Detail,... (the By cell is "You" or a person label).
const SIGN_IN_CSV_LINE = new RegExp(`^[^,]+,Account,${SIGN_IN_LABEL},[^,]*,${SIGN_IN_DETAIL},`, 'm');

interface ActivityRow { action: string; category: string; label: string; detail: string }

async function open(page: Page, path: string): Promise<void> {
  const resp = await page.goto(path, { waitUntil: 'domcontentloaded' });
  expect(resp?.status() ?? 0, `${path}: navigation failed`).toBeLessThan(400);
  await waitForPortalData(page);
}

test.describe('UI smoke — portal Account pages (AA-761)', () => {
  test.skip(() => !hasTenantState(), NO_LOGIN_REASON);

  test('Billing: rewrite meter with reset date, invoices empty state', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    const watcher = watchPage(page);
    try {
      const quota = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/tenant/v1/quota',
        { timeout: 20000 });
      await open(page, '/portal/account/billing');
      expect((await quota).status()).toBe(200);
      const meter = page.getByTestId('billing-usage-meter');
      await expect(meter).toBeVisible({ timeout: 15000 });
      await expect(meter).toContainText(/Resets \d{1,2} \w{3} \d{4}/);
      await expect(meter).toHaveAttribute('data-state', /^(no_plan|unlimited|ok|near|over)$/);
      await expect(page.getByTestId('billing-invoices').getByTestId('kit-empty-state')).toContainText('No invoices yet');
      await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa761-billing-1440.png`, fullPage: true });
      await watcher.settle();
      expect(watcher.apiFailures, describeApiFailures(watcher.apiFailures)).toEqual([]);
      expect(watcher.consoleErrors.map((e) => e.text)).toEqual([]);
    } finally {
      await context.close();
    }
  });

  test('Activity: real rows, type filter on the server, CSV export of the filtered set', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 1440, height: 900 }, acceptDownloads: true });
    await installBypassRoute(context);
    const page = await context.newPage();
    const watcher = watchPage(page);
    try {
      const first = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/tenant/v1/account/activity',
        { timeout: 20000 });
      await open(page, '/portal/account/activity');
      const body = await (await first).json();
      expect(body.pagination.total, 'the setup sign-in is in the trail').toBeGreaterThan(0);
      // The newest rows hold the partner sign-in the setup just made (newest first; nothing else signs this tenant in).
      const signIns = (body.data as ActivityRow[]).filter((r) => r.action === 'tenant.login');
      expect(signIns.length, 'a tenant.login row on the first page').toBeGreaterThan(0);
      expect(signIns[0]).toMatchObject({ category: 'account', label: SIGN_IN_LABEL, detail: SIGN_IN_DETAIL });
      // The table shows it: a row whose Activity cell reads "Signed in" over "Content studio".
      const signInRow = page.getByTestId('kit-datatable-row')
        .filter({ has: page.getByText(SIGN_IN_LABEL, { exact: true }) })
        .filter({ has: page.getByText(SIGN_IN_DETAIL, { exact: true }) });
      await expect(signInRow.first()).toBeVisible({ timeout: 15000 });

      const filtered = page.waitForResponse((r) => {
        const u = new URL(r.url());
        return u.pathname === '/api/tenant/v1/account/activity' && u.searchParams.get('category') === 'account';
      }, { timeout: 15000 });
      await page.getByLabel('Type').selectOption('account');
      const fb = await (await filtered).json();
      expect(fb.data.length, 'the account filter keeps the sign-in').toBeGreaterThan(0);
      expect((fb.data as ActivityRow[]).every((r) => r.category === 'account')).toBe(true);
      expect((fb.data as ActivityRow[]).some((r) => r.action === 'tenant.login')).toBe(true);

      const download = page.waitForEvent('download', { timeout: 20000 });
      await page.getByTestId('activity-export').click();
      const file = await download;
      expect(file.suggestedFilename()).toMatch(/^activity-account-\d{4}-\d{2}-\d{2}\.csv$/);
      const text = readFileSync(await file.path(), 'utf8');
      expect(text.replace(/^﻿/, '').split('\n')[0]).toBe(CSV_HEADER);
      expect(text, 'the CSV holds the partner sign-in row').toMatch(SIGN_IN_CSV_LINE);

      await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa761-activity-1440.png`, fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, 'Activity scrolls sideways at 390px').toBeLessThanOrEqual(0);
      await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa761-activity-390.png`, fullPage: true });
      await watcher.settle();
      expect(watcher.apiFailures, describeApiFailures(watcher.apiFailures)).toEqual([]);
      expect(watcher.consoleErrors.map((e) => e.text)).toEqual([]);
    } finally {
      await context.close();
    }
  });

  test('Activity: the pager asks the server for the next page', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const origin = new URL(baseUrl()).origin;
    const pagesAsked: string[] = [];
    await context.route('**/api/tenant/v1/account/activity*', async (route) => {
      const u = new URL(route.request().url());
      if (u.origin !== origin || u.pathname !== '/api/tenant/v1/account/activity') return route.fallback();
      const page = Number(u.searchParams.get('page') ?? '1');
      const size = Number(u.searchParams.get('page_size') ?? '25');
      pagesAsked.push(String(page));
      const data = Array.from({ length: size }, (_, i) => ({
        id: `row-${page}-${i}`, at: '2026-10-20T08:30:00+00:00', action: 'tenant.login', category: 'account',
        label: `Signed in (page ${page})`, actor: 'you', resource_type: 'tenant', resource_id: 'x', detail: 'Content studio',
      }));
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        data, pagination: { page, page_size: size, total: 60, pages: Math.ceil(60 / size) },
        categories: [{ id: 'content', label: 'Content' }, { id: 'tours', label: 'Tours' },
          { id: 'account', label: 'Account' }],
        actions: [],
      }) });
    });
    const page = await context.newPage();
    try {
      await open(page, '/portal/account/activity');
      await expect(page.getByText('Signed in (page 1)').first()).toBeVisible({ timeout: 15000 });
      // The kit pager's next button follows its "Page n / m" label (icon only, no name).
      await page.getByText(/^Page 1 \/ 3$/).locator('xpath=following-sibling::button[1]').click();
      await expect(page.getByText('Signed in (page 2)').first()).toBeVisible({ timeout: 15000 });
      expect(pagesAsked).toContain('2');
    } finally {
      await context.close();
    }
  });

  // AA-1036 R3 slice 3: the API key card went with the tenant API keys; the page shows no API section.
  test('Integrations: channel health, no API key card', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    const watcher = watchPage(page);
    try {
      await open(page, '/portal/account/integrations');
      await expect(page.getByTestId('wp-health')).toHaveAttribute('data-health',
        /^(not_connected|healthy|failing|untested)$/, { timeout: 15000 });
      await expect(page.getByTestId('api-key-card')).toHaveCount(0);
      await expect(page.getByRole('heading', { name: 'API access' })).toHaveCount(0);
      await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/aa761-integrations-1440.png`, fullPage: true });
      await watcher.settle();
      expect(watcher.apiFailures, describeApiFailures(watcher.apiFailures)).toEqual([]);
      expect(watcher.consoleErrors.map((e) => e.text)).toEqual([]);
    } finally {
      await context.close();
    }
  });
});
