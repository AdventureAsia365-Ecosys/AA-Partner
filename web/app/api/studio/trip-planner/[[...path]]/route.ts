// web/app/api/studio/trip-planner/[[...path]]/route.ts
// AA-1139 (fix pass) — the Content studio's Trip planner page, for an agency WITHOUT the CRM add-on (no CRM agency, so
// the CRM's /bff/trip-planner routes cannot serve it). Same paths and answer shapes as those BFF routes (the page
// reuses the CRM's view), two upstreams (lib/studio/tripPlanner.ts):
//   - CIS /v1/account/tripplanner[/lead-route|/appearance] with the verified studio token (requireStudio): the site
//     settings; CIS enforces the studio role (only the owner writes);
//   - the CRM API /crm/v1/studio/trip-planner[/leads|/webhook-key] with the person's own partner access token and the
//     studio's tenant (crmCall, lib/bff.ts SERVER_ONLY_ROUTES): TripPlanner's usage, leads and signing key; the API asks
//     CIS which studio membership the person holds (never crm.member).
// The studio role is checked here first (owner / editor read, owner changes), so a viewer reaches neither upstream.
// The tenant is the verified studio token's, never the request's. Cross-site writes are refused before any cookie is
// read (lib/writeGuard.ts). Answers are no-store; the signing key passes through once and is never logged.
import { NextRequest, NextResponse } from "next/server";
import { requestConfig } from "@/lib/config";
import { crmCall } from "@/lib/crmApi";
import { accessValid, readSession } from "@/lib/session";
import { requireStudio, withRenewed } from "@/lib/studio/auth-server";
import { studioClaims } from "@/lib/studio/studioSession";
import { cisDetail, plannerAction, studioMay, type PlannerAction } from "@/lib/studio/tripPlanner";
import { refuseForeignWrite } from "@/lib/writeGuard";

export const dynamic = "force-dynamic";

const CIS_TIMEOUT_MS = 10_000;
const NO_STORE = { "Cache-Control": "no-store" };

type Upstream = { ok: true; data: unknown } | { ok: false; status: number; detail: string };

function answer(body: unknown, status = 200): NextResponse {
  return NextResponse.json(body, { status, headers: NO_STORE });
}

async function cis(apiUrl: string, token: string, method: "GET" | "PUT", rest: string, body?: unknown): Promise<Upstream> {
  let res: Response;
  try {
    res = await fetch(`${apiUrl}/v1/account/tripplanner${rest}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(CIS_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, status: 503, detail: "tripplanner_unavailable" };
  }
  const json = (await res.json().catch(() => null)) as unknown;
  if (res.ok) return { ok: true, data: json };
  if (res.status >= 500) return { ok: false, status: 503, detail: "tripplanner_unavailable" };
  return { ok: false, status: res.status, detail: cisDetail(res.headers.get("x-error-code"), json) };
}

async function readBody(req: NextRequest): Promise<Record<string, unknown> | null> {
  try {
    const body = (await req.json()) as unknown;
    return body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

type Studio = Extract<Awaited<ReturnType<typeof requireStudio>>, { ok: true }>;

/** The CRM API call of the studio page: the person's own partner token, the studio's tenant (never the request's). */
function crmFor(req: NextRequest, auth: Studio) {
  const session = readSession(auth.cfg, req.cookies);
  return <T,>(method: "GET" | "POST", sub: string) =>
    session && accessValid(session)
      ? crmCall<T>(auth.cfg, { ...session, tid: auth.tenantId }, method, sub)
      : Promise.resolve({ ok: false as const, status: 401, detail: "session_expired" });
}

/** GET "": CIS's module, plus TripPlanner's usage and key version (null when not active or not answering). */
async function page(req: NextRequest, auth: Studio): Promise<NextResponse> {
  const site = await cis(auth.cfg.cisApiUrl, auth.token, "GET", "");
  if (!site.ok) return answer({ detail: site.detail }, site.status);
  const active = (site.data as { active?: unknown } | null)?.active === true;
  const tp = active ? await crmFor(req, auth)<{ usage: unknown; webhook_key: unknown }>("GET", "studio/trip-planner") : null;
  const parts = tp?.ok ? tp.data : { usage: null, webhook_key: null };
  return answer({ module: site.data, usage: parts.usage ?? null, webhook_key: parts.webhook_key ?? null });
}

/** PUT "lead-route" / "appearance": the owner's change, at CIS -> {module}. */
async function siteWrite(req: NextRequest, auth: Studio, action: "lead-route" | "appearance"): Promise<NextResponse> {
  const body = await readBody(req);
  if (!body) return answer({ detail: "invalid_body" }, 400);
  const site = await cis(auth.cfg.cisApiUrl, auth.token, "PUT", `/${action}`, body);
  return site.ok ? answer({ module: site.data }) : answer({ detail: site.detail }, site.status);
}

/** GET "leads" / POST "webhook-key": TripPlanner's part, through the CRM API. */
async function plannerPart(req: NextRequest, auth: Studio, action: "leads" | "webhook-key"): Promise<NextResponse> {
  const crm = crmFor(req, auth);
  const out = action === "leads"
    ? await crm<unknown>("GET", "studio/trip-planner/leads")
    : await crm<unknown>("POST", "studio/trip-planner/webhook-key");
  return out.ok ? answer(out.data) : answer({ detail: out.detail }, out.status === 0 ? 503 : out.status);
}

function run(req: NextRequest, auth: Studio, action: PlannerAction): Promise<NextResponse> {
  if (action === "page") return page(req, auth);
  if (action === "lead-route" || action === "appearance") return siteWrite(req, auth, action);
  return plannerPart(req, auth, action);
}

async function handler(req: NextRequest, { params }: { params: Promise<{ path?: string[] }> }) {
  const refused = refuseForeignWrite(requestConfig(req), req);
  if (refused) return refused;
  const { path } = await params;
  const action = plannerAction(req.method, path ?? []);
  if (!action) return answer({ detail: "not_found" }, 404);
  const auth = await requireStudio(req);
  if (!auth.ok) return auth.response;
  const res = studioMay(studioClaims(auth.token).csr, action) ? await run(req, auth, action) : answer({ detail: "role_forbidden" }, 403);
  return withRenewed(res, auth.renewed, auth.cfg);
}

export const GET = handler;
export const PUT = handler;
export const POST = handler;
