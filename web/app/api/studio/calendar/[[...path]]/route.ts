// web/app/api/studio/calendar/[[...path]]/route.ts
// AA-1011 follow-up (S294) — the Content studio's Calendar card (Account > Integrations): the member's own Google /
// Microsoft 365 calendar, into which the CRM pushes departures and tasks. The card used the CRM's browser BFF, whose
// tenant routes need the agency a CRM session picked; a studio session never picks one, so the page logged a 409
// `no_agency_selected` on load (Deploy Web Dev studio smoke, rolled back). This handler calls the CRM API with the
// person's own partner access token and the verified studio token's tenant (never the request's); the CRM API checks
// the crm.member row of that agency (tenant_tx), as on every CRM route. Paths and answers: lib/studio/calendar.ts.
// The read is display only and answers 200 "not available" for a refusal (no CRM role, no CRM agency, a locked
// agency); the writes pass the CRM's answer through. Cross-site writes are refused before any cookie is read
// (lib/writeGuard.ts). Answers are no-store; nothing is logged.
import { NextRequest } from "next/server";
import { requestConfig } from "@/lib/config";
import { crmCall } from "@/lib/crmApi";
import { json } from "@/lib/respond";
import { accessValid, readSession } from "@/lib/session";
import { requireStudio, withRenewed } from "@/lib/studio/auth-server";
import { calendarRoute, viewAnswer, writeAnswer, type CalendarRoute } from "@/lib/studio/calendar";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 16 * 1024; // an OAuth code + state (4 KiB each at the API) fit comfortably

/** The JSON object body of a write, or null when it is missing, too large or not an object. */
async function readBody(req: NextRequest): Promise<Record<string, unknown> | null> {
  const text = await req.text();
  if (Buffer.byteLength(text) > MAX_BODY_BYTES) return null;
  try {
    const body = JSON.parse(text) as unknown;
    return body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

type Studio = Extract<Awaited<ReturnType<typeof requireStudio>>, { ok: true }>;

/** The CRM API call of one route: the person's own partner token, the studio's tenant (never the request's). */
async function forward(req: NextRequest, auth: Studio, route: CalendarRoute, body: Record<string, unknown> | undefined) {
  const session = readSession(auth.cfg, req.cookies);
  if (!session || !accessValid(session)) return { ok: false, status: 401, detail: "session_expired" } as const;
  return crmCall<unknown>(auth.cfg, { ...session, tid: auth.tenantId }, route.method, route.sub, body === undefined ? {} : { body });
}

async function handler(req: NextRequest, { params }: { params: Promise<{ path?: string[] }> }) {
  const refused = refuseForeignWrite(requestConfig(req), req);
  if (refused) return refused;
  const { path } = await params;
  const route = calendarRoute(req.method, path ?? []);
  if (!route) return json({ detail: "not_found" }, 404);
  const auth = await requireStudio(req);
  if (!auth.ok) return auth.response;
  const body = route.body ? await readBody(req) : undefined;
  if (body === null) return withRenewed(json({ detail: "invalid_body" }, 400), auth.renewed, auth.cfg);
  const out = await forward(req, auth, route, body);
  const answer = route.view ? viewAnswer(out) : writeAnswer(out);
  return withRenewed(json(answer.body, answer.status), auth.renewed, auth.cfg);
}

export const GET = handler;
export const POST = handler;
