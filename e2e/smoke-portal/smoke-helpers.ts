// e2e/smoke-portal/smoke-helpers.ts
// AA-979 (CRM-P2a) — the generic helpers of the portal UI smoke, trimmed from AA-CIS-App
// tests/e2e/smoke/smoke-helpers.ts (AA-732 / AA-885) when the portal moved here: only what the portal specs use
// (base URL, origin-scoped bypass route, page watcher, content check, page check). The admin-only parts (staff role,
// rbac page matrix, admin sidebar width and dark-theme checks) stay in AA-CIS-App. Same code otherwise.
//
// What a "page check" means (skill aa-ui-verify): the page navigates with an OK status, logs no console errors, makes
// no failing same-origin /api/ call, and shows real content (a kit/table row, the kit empty state or the page's own
// ready selector). Paths are relative to e2e/ (the workflow's working directory).
import { expect, type BrowserContext, type Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';

// Auth storage state holds LIVE session cookies: keep it out of the results the workflow uploads and out of git
// (e2e/.gitignore). The workflow deletes it after the run.
export const AUTH_DIR = '.auth';
export const TENANT_STATE = path.join(AUTH_DIR, 'tenant-state.json');

/** Known-benign console messages. Empty by default — add a precise substring to silence real
 * third-party noise, never to hide an app error. */
export const CONSOLE_ALLOWLIST: string[] = [];

export type PageSpec = {
  /** Short slug for screenshot filenames. */
  id: string;
  /** Human label used in test titles. */
  label: string;
  /** Route to navigate to (path only; BASE_URL is prepended by Playwright). */
  path: string;
  /** Optional CSS selector that marks "this page's real content has rendered". It MUST render only after data has
   * loaded, never on the loading skeleton, so a blank/broken page still fails. */
  readySelector?: string;
};

export function baseUrl(): string {
  return process.env.BASE_URL || 'http://localhost:3004';
}

export function baseOrigin(): string {
  try {
    return new URL(baseUrl()).origin;
  } catch {
    return baseUrl();
  }
}

/**
 * Install the Vercel protection-bypass as an origin-scoped route, NOT context-level
 * extraHTTPHeaders. The secret must only ever reach the BASE_URL origin — never a third-party
 * host the page talks to. Requests to any other origin pass through untouched. No-op when the
 * secret is absent (local run).
 */
export async function installBypassRoute(context: BrowserContext): Promise<void> {
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  if (!secret) return;
  const origin = baseOrigin();
  await context.route('**/*', async (route) => {
    const req = route.request();
    // The route handler fires per request with that request's OWN URL — a cross-origin redirect
    // (e.g. BASE_URL → an SSO host) is a NEW request the handler re-evaluates by its new URL, so
    // the bypass header is never carried off-origin. Only requests whose origin equals BASE_URL's
    // get the header.
    let reqOrigin = '';
    try {
      reqOrigin = new URL(req.url()).origin;
    } catch {
      reqOrigin = '';
    }
    if (reqOrigin === origin) {
      const headers = {
        ...req.headers(),
        'x-vercel-protection-bypass': secret,
        // Keep the bypass cookie so client-side navigations stay bypassed.
        'x-vercel-set-bypass-cookie': 'samesitenone',
      };
      await route.continue({ headers });
    } else {
      await route.continue();
    }
  });
}

export type ApiFailure = { url: string; status: number; bodyPreview: string };
export type ConsoleError = { url: string; text: string };

export type PageWatcher = {
  consoleErrors: ConsoleError[];
  apiFailures: ApiFailure[];
  /** Resolve after all in-flight API body reads have completed. Call before asserting. */
  settle: () => Promise<void>;
};

/**
 * Attach listeners that record console errors and failing SAME-ORIGIN `/api/` responses. Body
 * reads are async, so we track their promises and expose `settle()` to await them before the
 * assertion (otherwise a late 500 can be missed). S212: read the body — a 500 once looked like a
 * 401.
 */
export function watchPage(page: Page): PageWatcher {
  const consoleErrors: ConsoleError[] = [];
  const apiFailures: ApiFailure[] = [];
  const pending: Promise<void>[] = [];
  const origin = baseOrigin();

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (CONSOLE_ALLOWLIST.some((a) => text.includes(a))) return;
    consoleErrors.push({ url: page.url(), text });
  });
  page.on('pageerror', (err) => {
    consoleErrors.push({ url: page.url(), text: err.message });
  });

  page.on('response', (res) => {
    const url = res.url();
    let u: URL;
    try {
      u = new URL(url);
    } catch {
      return;
    }
    // Same-origin API calls the page itself makes.
    if (u.origin !== origin || !u.pathname.startsWith('/api/')) return;
    const status = res.status();
    if (status < 400) return;
    pending.push(
      (async () => {
        let bodyPreview = '';
        try {
          bodyPreview = (await res.text()).slice(0, 300);
        } catch {
          bodyPreview = '<body unavailable>';
        }
        apiFailures.push({ url, status, bodyPreview });
      })(),
    );
  });

  return {
    consoleErrors,
    apiFailures,
    settle: async () => {
      // Bounded: a streaming / long-poll body would otherwise never resolve.
      await Promise.race([Promise.all(pending), new Promise((r) => setTimeout(r, 10000))]);
    },
  };
}

/**
 * True when the page shows ≥1 data row or the kit empty state — "page rendered real content, not a
 * blank/broken shell". Kit testids first; then a legacy `table tbody tr` fallback (legacy pages have no <main>; the sidebar has no table) that
 * excludes the kit's loading skeleton (the kit renders its skeleton as shimmer <div>s, NOT table
 * rows, so a real `<tbody><tr>` is genuine data); then the exact kit empty-state wording scoped to
 * `main`. No broad text regex.
 */
export async function hasContentOrEmptyState(page: Page, readySelector?: string): Promise<boolean> {
  // AA-601 — a page-specific "content rendered" signal (card lists, config view, custom empty
  // state). It renders only after data loads, never on the skeleton, so it is a genuine signal.
  if (readySelector && (await page.locator(readySelector).count()) > 0) return true;

  // Kit DataTable row / kit EmptyState — the robust UI-v2 signal.
  if ((await page.locator('[data-testid="kit-datatable-row"]').count()) > 0) return true;
  if ((await page.locator('[data-testid="kit-empty-state"]').count()) > 0) return true;

  // Legacy fallback: a real data row inside a <main> table body (kit skeleton is <div>, not <tr>).
  if ((await page.locator('table tbody tr').count()) > 0) return true;

  // Legacy empty-state copy — exact phrases only (no broad regex).
  const emptyPhrases = ['Nothing here yet', 'No results', 'No data'];
  for (const phrase of emptyPhrases) {
    if ((await page.locator('body', { hasText: phrase }).count()) > 0) return true;
  }
  return false;
}

/** Wait for the kit loading skeleton to resolve into real content (data row, empty state, or a
 * legacy table row) before reading content. Bounded + non-fatal. */
export async function waitForSkeletonGone(page: Page, readySelector?: string): Promise<void> {
  // The kit shows a shimmer skeleton (not a <tbody>) while loading; once data (or empty) resolves,
  // either the tbody appears, the EmptyState renders, or the page's own readySelector appears. Give
  // it a bounded wait, non-fatal.
  await page
    .waitForFunction(
      (sel) => {
        const hasBody = document.querySelector('[data-testid="kit-datatable-body"]');
        const hasEmpty = document.querySelector('[data-testid="kit-empty-state"]');
        const hasLegacy = document.querySelector('table tbody tr');
        const hasReady = sel ? document.querySelector(sel) : null;
        return Boolean(hasBody || hasEmpty || hasLegacy || hasReady);
      },
      readySelector ?? null, // waitForFunction(fn, arg, options) — options is the THIRD param
      { timeout: 8000 },
    )
    .catch(() => {});
}

export function ensureAuthDir(): void {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
}

/** Build a failure message that embeds the recorded API failures (URL + status + body preview). */
export function describeApiFailures(failures: ApiFailure[]): string {
  return failures
    .map((f) => `  ${f.status} ${f.url}\n    body: ${f.bodyPreview.replace(/\n/g, ' ')}`)
    .join('\n');
}

/** Navigate and assert the main response was OK (2xx/3xx), then let client fetches settle. */
export async function assertNavOk(page: Page, pathToVisit: string): Promise<void> {
  const resp = await page.goto(pathToVisit, { waitUntil: 'domcontentloaded' });
  expect(resp, `no response for ${pathToVisit}`).not.toBeNull();
  const status = resp!.status();
  expect(status, `navigation to ${pathToVisit} returned HTTP ${status}`).toBeLessThan(400);
  // Bounded: a page that polls (or keeps a long-lived request open) never reaches networkidle, and an
  // unbounded wait burned the whole 60 s test timeout on Master Content (S220 live run).
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
}

export type PageCheckOptions = {
  /** Extra assertions on the loaded page (e.g. the portal's 390px overflow check). */
  afterChecks?: (page: Page, spec: PageSpec) => Promise<void>;
  /** Screenshot capture (the portal passes portalScreenshots); none when absent. */
  screenshots?: (page: Page, slug: string) => Promise<void>;
};

/**
 * Page check: open the page in its OWN context from `storageStatePath` (tests stay independent), then assert an OK
 * navigation, no failing same-origin /api/ response, real content (row, empty state or ready selector), no console
 * error; then the optional extra checks and the screenshots.
 */
export async function runPageCheck(
  browser: import('@playwright/test').Browser,
  storageStatePath: string,
  spec: PageSpec,
  opts: PageCheckOptions = {},
): Promise<void> {
  const context = await browser.newContext({
    baseURL: baseUrl(),
    storageState: storageStatePath,
    viewport: { width: 1440, height: 900 },
  });
  await installBypassRoute(context);
  const page = await context.newPage();
  const watcher = watchPage(page);
  try {
    await assertNavOk(page, spec.path);
    await waitForSkeletonGone(page, spec.readySelector);

    // Let every in-flight same-origin /api/ body read complete before asserting.
    await watcher.settle();

    expect(
      watcher.apiFailures,
      `${spec.label}: failing same-origin /api/ responses:\n${describeApiFailures(watcher.apiFailures)}`,
    ).toEqual([]);

    const ok = await hasContentOrEmptyState(page, spec.readySelector);
    expect(ok, `${spec.label}: no table rows and no empty state — page looks blank/broken`).toBe(true);

    expect(
      watcher.consoleErrors,
      `${spec.label}: console errors:\n${watcher.consoleErrors.map((e) => `  ${e.text}`).join('\n')}`,
    ).toEqual([]);

    if (opts.afterChecks) {
      await opts.afterChecks(page, spec);
    }
    if (opts.screenshots) {
      await opts.screenshots(page, spec.id);
    }
  } finally {
    await context.close();
  }
}
