// e2e/smoke-portal/aa1188-tenant-proxy-allowlist.spec.ts
// AA-1188 (S295 data-boundary audit, DB-01) — the studio BFF (/api/tenant/*) forwards only the CIS tenant calls the
// studio makes (web/lib/studio/tenantRoutes.ts). An unlisted path, such as the raw tour row GET /v1/tours/{id}/full
// (supplier name, source price text, pipeline state), is 404 before the studio token is read or CIS is called, signed
// out and signed in. Read only: nothing here writes, enqueues or meters.
import { test, expect } from '@playwright/test';
import { baseUrl, installBypassRoute } from './smoke-helpers';
import { NO_LOGIN_REASON, TENANT_STATE, hasTenantState } from './portal-helpers';

const TOUR = '00000000-0000-0000-0000-000000000000';
const REFUSED = [`/api/tenant/v1/tours/${TOUR}/full`, `/api/tenant/v1/tours/${TOUR}`, '/api/tenant/openapi.json'];

test.describe('AA-1188 — the studio BFF forwards only listed CIS calls', () => {
  test('signed out: an unlisted path is 404, a listed one stops at the sign-in check (401)', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl() });
    await installBypassRoute(context);
    try {
      for (const p of REFUSED) {
        const res = await context.request.get(p, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${p}`).toBe(404);
      }
      const listed = await context.request.get('/api/tenant/v1/billing', { maxRedirects: 0, timeout: 15000 });
      expect(listed.status(), 'GET /api/tenant/v1/billing').toBe(401);
    } finally {
      await context.close();
    }
  });

  test.describe('signed in', () => {
    test.skip(() => !hasTenantState(), NO_LOGIN_REASON);

    test('an unlisted path is still 404; a listed read answers 200', async ({ browser }) => {
      const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE });
      await installBypassRoute(context);
      try {
        for (const p of REFUSED) {
          const res = await context.request.get(p, { maxRedirects: 0, timeout: 15000 });
          expect(res.status(), `GET ${p}`).toBe(404);
        }
        const listed = await context.request.get('/api/tenant/v1/quota', { timeout: 15000 });
        expect(listed.status(), 'GET /api/tenant/v1/quota').toBe(200);
      } finally {
        await context.close();
      }
    });
  });
});
