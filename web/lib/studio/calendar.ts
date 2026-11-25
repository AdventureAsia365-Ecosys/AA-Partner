// web/lib/studio/calendar.ts
// AA-1011 follow-up (S294) — the Content studio's Calendar card (Account > Integrations) talks to the CRM API through
// the studio's own route handler (app/api/studio/calendar/[[...path]]), never the CRM's browser BFF: a studio session
// has no CRM agency picked (session.tid), so every tenant /bff route answered 409 `no_agency_selected` on page load.
// The handler sends the person's partner token with the studio's verified tenant; the CRM API checks the crm.member
// row of that agency as on every CRM route.
//   GET  ""                      -> GET  /crm/v1/calendar/connections   (display: always 200, see viewAnswer)
//   POST "oauth/start"           -> POST /crm/v1/calendar/connections/oauth/start     (the CRM's answer passes through)
//   POST "oauth/complete"        -> POST /crm/v1/calendar/connections/oauth/complete
//   POST "<uuid>/disconnect"     -> POST /crm/v1/calendar/connections/<uuid>/disconnect
// Pure helpers here (tests/studioCalendar.test.ts).
import type { CalendarConnectionsView } from "../calendarSync";

export const STUDIO_CALENDAR_API = "/api/studio/calendar";

export interface CalendarRoute {
  method: "GET" | "POST";
  /** The CRM API path under /crm/v1 (a SERVER_ONLY_ROUTES line of lib/bff.ts). */
  sub: string;
  /** The display read (answers 200 "not available" instead of a refusal). */
  view: boolean;
  /** The request carries a JSON body to pass on. */
  body: boolean;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** The route of (method, path segments after /api/studio/calendar), or null (404). */
export function calendarRoute(method: string, segments: readonly string[]): CalendarRoute | null {
  const m = method.toUpperCase();
  const rest = segments.join("/");
  if (m === "GET" && rest === "") return { method: "GET", sub: "calendar/connections", view: true, body: false };
  if (m === "POST" && (rest === "oauth/start" || rest === "oauth/complete")) {
    return { method: "POST", sub: `calendar/connections/${rest}`, view: false, body: true };
  }
  if (m === "POST" && segments.length === 2 && UUID_RE.test(segments[0]) && segments[1] === "disconnect") {
    return { method: "POST", sub: `calendar/connections/${segments[0]}/disconnect`, view: false, body: false };
  }
  return null;
}

/** The view of a person the CRM cannot serve here: no CRM role in this agency, no CRM agency, a locked agency. */
export const NO_CRM_CALENDAR: CalendarConnectionsView = {
  connections: [],
  providers: { google: false, microsoft: false },
  kinds: [],
  available: false,
};

/** CRM refusals that mean "this person has no calendar here", not a failure of the page. */
const NOT_AVAILABLE = new Set([400, 403, 404, 409]);

type Upstream = { ok: true; status: number; data: unknown } | { ok: false; status: number; detail: string };

export interface Answer {
  status: number;
  body: unknown;
}

/** The display read's answer: the CRM's view (available), the 200 "not available" view for a refusal (root lesson
 *  S264: a display route never answers a "normal" 4xx, the browser logs it), 401 to sign in again, else 503. */
export function viewAnswer(out: Upstream): Answer {
  if (out.ok) return { status: 200, body: { ...(out.data as object), available: true } };
  if (out.status === 401) return { status: 401, body: { detail: "session_expired" } };
  if (NOT_AVAILABLE.has(out.status)) return { status: 200, body: NO_CRM_CALENDAR };
  return { status: 503, body: { detail: "calendar_unavailable" } };
}

/** A write's answer: the CRM's own status and body (the card words its error codes); unreachable is 503. */
export function writeAnswer(out: Upstream): Answer {
  if (out.ok) return { status: out.status, body: out.data };
  return { status: out.status === 0 ? 503 : out.status, body: { detail: out.detail } };
}

/** The studio handler URL of a card call ("" for the view, "oauth/start", "<id>/disconnect", ...). */
export function studioCalendarUrl(rest: string): string {
  return rest ? `${STUDIO_CALENDAR_API}/${rest}` : STUDIO_CALENDAR_API;
}
