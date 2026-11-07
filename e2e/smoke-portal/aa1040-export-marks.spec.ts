// e2e/smoke-portal/aa1040-export-marks.spec.ts
// AA-1040 (EU AI Act Art. 50(2)) — the Content studio downloads keep their machine-readable AI mark through the
// agency app's studio BFF (/api/tenant/*, web/lib/studio/downloadHeaders.ts), logged in as the e2e studio user
// (storage state from auth.setup.ts):
//   - a ready blog piece exported as an HTML document and as a fragment: X-AI-Generated true + X-AI-Generator headers,
//     `data-ai-generated="true"` on the <article> and `<meta name="ai-generated" content="true">` in the body;
//   - a ready piece exported as text (no markup): the headers are its mark;
//   - a T2 version's DOCX: a `.docx` Content-Disposition file name (its AI mark is in the file's core properties).
// Calls go through the page context's request (same cookies as the page, lesson S243/S251), as the studio's own
// fetch does. Each export writes one CIS audit row, exactly like a person clicking Export; nothing is published and
// no model is called. A tenant without a ready piece or version gets an annotation, not a failure.
import { test, expect, type APIRequestContext, type APIResponse } from '@playwright/test';
import { baseUrl, installBypassRoute } from './smoke-helpers';
import { NO_LOGIN_REASON, TENANT_STATE, hasTenantState } from './portal-helpers';

type ReviewRow = { piece_id: string; channel?: string | null; ready_state?: string | null; content_text?: string | null };
type VersionRow = { id?: string; version_id?: string };

const PIECES = '/api/tenant/v1/content-writing/pieces';

function expectAiHeaders(res: APIResponse, where: string): void {
  const h = res.headers();
  expect(h['x-ai-generated'], `${where}: X-AI-Generated`).toBe('true');
  expect((h['x-ai-generator'] ?? '').length, `${where}: X-AI-Generator`).toBeGreaterThan(0);
}

async function readyPieces(request: APIRequestContext): Promise<ReviewRow[]> {
  const res = await request.get('/api/tenant/v1/content-writing/reviews');
  expect(res.status(), 'GET /v1/content-writing/reviews').toBe(200);
  const body = (await res.json()) as { data?: ReviewRow[] };
  return (body.data ?? []).filter((r) => r.ready_state === 'ready' && r.content_text != null);
}

test.describe('AA-1040 — studio downloads keep the AI mark', () => {
  test.skip(() => !hasTenantState(), NO_LOGIN_REASON);

  test('piece exports: X-AI-* headers and the in-body marks pass the studio BFF', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE });
    await installBypassRoute(context);
    try {
      const request = context.request;
      const pieces = await readyPieces(request);
      const blog = pieces.find((r) => r.channel === 'blog');
      if (blog) {
        for (const mode of ['document', 'fragment'] as const) {
          const res = await request.get(`${PIECES}/${blog.piece_id}/export?format=html&mode=${mode}`);
          expect(res.status(), `HTML ${mode} export`).toBe(200);
          expectAiHeaders(res, `HTML ${mode}`);
          const html = await res.text();
          expect(html, `HTML ${mode}: <article data-ai-generated>`).toMatch(/<article[^>]*\sdata-ai-generated="true"/);
          expect(html, `HTML ${mode}: <meta name="ai-generated">`).toContain('<meta name="ai-generated" content="true">');
        }
      } else {
        test.info().annotations.push({ type: 'aa1040', description: 'the e2e tenant has no ready blog piece: HTML export not checked' });
      }
      const textPiece = pieces.find((r) => r.channel !== 'blog') ?? blog;
      if (textPiece) {
        const res = await request.get(`${PIECES}/${textPiece.piece_id}/export?format=text`);
        expect(res.status(), 'text export').toBe(200);
        expectAiHeaders(res, 'text');
      } else {
        test.info().annotations.push({ type: 'aa1040', description: 'the e2e tenant has no ready piece: text export not checked' });
      }
    } finally {
      await context.close();
    }
  });

  test('T2 DOCX export: the file name passes the studio BFF', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: TENANT_STATE });
    await installBypassRoute(context);
    try {
      const request = context.request;
      const list = await request.get('/api/tenant/v1/tours/my-versions?page_size=5');
      expect(list.status(), 'GET /v1/tours/my-versions').toBe(200);
      const versions = ((await list.json()) as { data?: VersionRow[] }).data ?? [];
      const ids = versions.map((v) => v.id ?? v.version_id).filter((id): id is string => !!id).slice(0, 3);
      for (const id of ids) {
        const res = await request.get(`/api/tenant/v1/tours/versions/${id}/export-docx`);
        if (res.status() !== 200) continue; // a version still being written has nothing to export yet
        expect(res.headers()['content-disposition'] ?? '', 'DOCX Content-Disposition')
          .toMatch(/^attachment; filename="[A-Za-z0-9][A-Za-z0-9._-]*\.docx"$/);
        expect((await res.body()).subarray(0, 2).toString('latin1'), 'DOCX is a zip').toBe('PK');
        return;
      }
      test.info().annotations.push({ type: 'aa1040', description: 'the e2e tenant has no exportable T2 version: DOCX not checked' });
    } finally {
      await context.close();
    }
  });
});
