// e2e/smoke-portal/s251-my-content-table.spec.ts
// S251 portal UI check (AA-669) — My Content's desktop table, logged in as the test tenant (storage state from
// auth.setup.ts). Read only: nothing here writes, exports or publishes.
//
//   - At 1440 x 900 the table fits its scroll box: no sideways scroll (`scrollWidth <= clientWidth` of the kit
//     wrapper), every row's actions inside the box, and the title column keeps a real width (it had collapsed to
//     ~68 px and broke words mid-letter, D1).
//   - The angle's internal writing brief (`formula_fit`, "Hook-Insight-CTA: ...") is never rendered, on the
//     desktop table nor on the 390 px cards (D2): checked against the brief texts the API returns for this tenant,
//     and against the brief's shape ("Word-Word-Word:").
//   - The sorted header carries aria-sort (D4): the default view sorts by Created, newest first.
//
// Scrollbars stay on (lesson S239: headless Chromium hides them, so a table that "just fits" scrolls on a real
// screen); the box must keep >= 20 px to spare. A tenant without content only gets the aria-sort and brief checks
// on whatever renders (annotated).
import { test, expect, type Page } from '@playwright/test';
import { baseUrl, installBypassRoute, watchPage, describeApiFailures } from './smoke-helpers';
import {
  NO_KEY_REASON,
  PORTAL_RESULTS_DIR,
  TENANT_STATE,
  ensurePortalResultsDir,
  hasTenantState,
  waitForPortalData,
} from './portal-helpers';

ensurePortalResultsDir();

test.use({ launchOptions: { ignoreDefaultArgs: ['--hide-scrollbars'] } });

/** The writing-brief shape: three or more capitalised words joined by hyphens, then a colon. */
const BRIEF_SHAPE = /\b[A-Z][a-z]+(?:-[A-Z][a-z]+){2,}\s*(?:\([A-Z]+\))?\s*:/;

type ReviewRow = { piece_id: string; angle?: { name?: string; formula_fit?: string } | null };

async function openMyContent(page: Page): Promise<ReviewRow[]> {
  const reviews = page.waitForResponse(
    (r) => new URL(r.url()).pathname === '/api/tenant/v1/content-writing/reviews', { timeout: 20000 });
  const resp = await page.goto('/portal/content', { waitUntil: 'domcontentloaded' });
  expect(resp?.status() ?? 0, '/portal/content: navigation failed').toBeLessThan(400);
  const answer = await reviews;
  expect(answer.status(), 'GET /v1/content-writing/reviews').toBe(200);
  const body = (await answer.json()) as { data?: ReviewRow[] };
  await waitForPortalData(page);
  await expect(page.locator('main h1')).toBeVisible({ timeout: 15000 });
  return body.data ?? [];
}

/** The brief texts the API holds for this tenant (their first 40 characters, enough to be unique). */
function briefSnippets(rows: ReviewRow[]): string[] {
  return rows
    .map((r) => (r.angle?.formula_fit ?? '').trim())
    .filter((t) => t.length >= 20)
    .map((t) => t.slice(0, 40));
}

async function assertNoBrief(page: Page, rows: ReviewRow[], where: string): Promise<void> {
  const text = await page.locator('main').innerText();
  for (const snippet of briefSnippets(rows)) {
    expect(text.includes(snippet), `${where}: the angle brief is rendered ("${snippet}...")`).toBe(false);
  }
  const shaped = text.match(BRIEF_SHAPE);
  expect(shaped, `${where}: text shaped like a writing brief is rendered ("${shaped?.[0]}")`).toBeNull();
}

test.describe('UI smoke — portal My Content table (S251)', () => {
  test.skip(() => !hasTenantState(), NO_KEY_REASON);

  test('1440: the table fits without a sideways scroll, titles keep their width, no angle brief', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    const watcher = watchPage(page);
    try {
      const rows = await openMyContent(page);
      const tableRows = page.getByTestId('kit-datatable-row');
      if (rows.length === 0) {
        test.info().annotations.push({ type: 'my-content', description: 'the test tenant has no content: layout not measured' });
      } else {
        await expect(tableRows.first()).toBeVisible({ timeout: 15000 });
        const m = await page.evaluate(() => {
          const table = document.querySelector<HTMLTableElement>('main table');
          const box = table?.parentElement;
          if (!table || !box) return null;
          const boxRect = box.getBoundingClientRect();
          const rows = Array.from(table.querySelectorAll<HTMLTableRowElement>('tbody tr'));
          const actionsRight = rows.map((tr) => {
            const buttons = Array.from(tr.querySelectorAll<HTMLElement>('td:last-child button'));
            return buttons.length ? Math.max(...buttons.map((b) => b.getBoundingClientRect().right)) : 0;
          });
          const titleWidths = Array.from(document.querySelectorAll<HTMLElement>('[data-testid="my-content-title"]'))
            .map((el) => el.getBoundingClientRect().width);
          // The table's own min-content width: what it needs before anything has to wrap or scroll.
          const prev = table.style.width;
          table.style.width = 'min-content';
          const minContent = table.getBoundingClientRect().width;
          table.style.width = prev;
          return {
            scrollWidth: box.scrollWidth,
            clientWidth: box.clientWidth,
            boxRight: Math.round(boxRect.right),
            maxActionsRight: Math.round(Math.max(0, ...actionsRight)),
            minTitleWidth: Math.round(Math.min(...titleWidths)),
            minContent: Math.round(minContent),
            rowHeights: rows.map((tr) => Math.round(tr.getBoundingClientRect().height)),
          };
        });
        expect(m, 'the My Content table did not render').not.toBeNull();
        test.info().annotations.push({ type: 'my-content-1440', description: JSON.stringify(m) });
        expect(m!.scrollWidth, `table scrolls sideways at 1440 (scrollWidth ${m!.scrollWidth} > clientWidth ${m!.clientWidth})`)
          .toBeLessThanOrEqual(m!.clientWidth);
        expect(m!.minContent + 20, `less than 20 px to spare (min-content ${m!.minContent}, box ${m!.clientWidth})`)
          .toBeLessThanOrEqual(m!.clientWidth);
        expect(m!.maxActionsRight, 'row actions are cut off on the right').toBeLessThanOrEqual(m!.boxRight);
        expect(m!.minTitleWidth, 'the title column collapsed').toBeGreaterThanOrEqual(150);
        expect(Math.max(...m!.rowHeights), 'a row is taller than a screen third (long cell text)').toBeLessThan(300);
      }
      await assertNoBrief(page, rows, '1440');
      await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/s251-my-content-1440.png` });
      await watcher.settle();
      expect(watcher.apiFailures, describeApiFailures(watcher.apiFailures)).toEqual([]);
      expect(watcher.consoleErrors.map((e) => e.text)).toEqual([]);
    } finally {
      await context.close();
    }
  });

  test('390: the cards show no angle brief and the date range stays on one line', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 390, height: 844 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      const rows = await openMyContent(page);
      await assertNoBrief(page, rows, '390');
      // D6: "From", "to" and "To" share one line, inside the screen.
      const range = await page.evaluate(() => {
        const box = document.querySelector<HTMLElement>('[data-testid="my-content-date-range"]');
        const from = box?.querySelector<HTMLElement>('input[aria-label="From"]')?.getBoundingClientRect();
        const to = box?.querySelector<HTMLElement>('input[aria-label="To"]')?.getBoundingClientRect();
        return from && to ? { fromTop: Math.round(from.top), toTop: Math.round(to.top), toRight: Math.round(to.right),
          innerWidth: window.innerWidth } : null;
      });
      expect(range, 'the date range filter did not render').not.toBeNull();
      expect(Math.abs(range!.fromTop - range!.toTop), 'From and To are on different lines').toBeLessThanOrEqual(2);
      expect(range!.toRight, 'the To field runs off the screen').toBeLessThanOrEqual(range!.innerWidth);
      await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/s251-my-content-390.png` });
    } finally {
      await context.close();
    }
  });

  test('the sorted header says so to a screen reader (aria-sort)', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      const rows = await openMyContent(page);
      test.skip(rows.length === 0, 'the test tenant has no content: no table header to check');
      const created = page.locator('main th', { hasText: /^Created$/i });
      await expect(created).toHaveAttribute('aria-sort', 'descending', { timeout: 15000 });
      await expect(page.locator('main th[aria-sort]')).toHaveCount(1);
      const words = page.locator('main th', { hasText: /^Words$/i });
      await words.click();
      await expect(words).toHaveAttribute('aria-sort', /^(ascending|descending)$/);
      await expect(created).not.toHaveAttribute('aria-sort', /.+/);
    } finally {
      await context.close();
    }
  });
});
