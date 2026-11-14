// web/lib/academyBff.ts
// AA-1105 — the academy BFF allow-list. The academy pages' browser code calls /academy/bff/<path> on the academy
// origin; the handler (app/academy/bff/[...path]/route.ts) forwards ONLY the (method, path) pairs listed here, and
// only onto the CIS learning API /learn/v1/<path> (contract learn.http.v1, root docs/contracts/), with the ACADEMY
// session's access token. CIS checks the token class (WORKFORCE x academy-web) and the learner on every call.
//
// Server components read through lib/academyApi.ts with the same paths. Adding a browser call = adding one line here.
// Edge-safe: no Node API.

export type AcademyMethod = "GET" | "POST";

export interface AcademyBffRoute {
  method: AcademyMethod;
  /** Matched against the encoded sub-path after /academy/bff/ (lib/bffPath.ts proxyPath). Anchored. */
  pattern: RegExp;
}

// The id shapes of learn.http.v1 (PathId, ModuleId, LessonId, review item_id, uuid).
export const PATH_ID = "[a-z][a-z0-9-]{1,40}";
export const MODULE_ID = "[A-Z][0-9]{1,2}";
export const LESSON_ID = "[A-Z][0-9]{1,2}-[0-9]{2}";
export const ITEM_ID = "[A-Z][0-9]{1,2}-Q[0-9]{1,3}";
export const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
export const CERT_CODE = "[A-Za-z0-9_-]{22}";

/** The upstream prefix: the learner route class only (never /learn/public, never another CIS class). */
export const LEARN_API_PREFIX = "/learn/v1";
/** The public verify route (no token): used by the verify page's server code only, never through the BFF. */
export const LEARN_PUBLIC_VERIFY_PREFIX = "/learn/public/v1/certificates";

const re = (s: string) => new RegExp(`^${s}$`);

export const ACADEMY_BFF_ROUTES: readonly AcademyBffRoute[] = [
  { method: "GET", pattern: re("me") },
  { method: "GET", pattern: re("paths") },
  { method: "POST", pattern: re(`paths/${PATH_ID}/enroll`) },
  { method: "GET", pattern: re(`modules/${MODULE_ID}`) },
  { method: "GET", pattern: re(`lessons/${LESSON_ID}`) },
  { method: "GET", pattern: re(`lessons/${LESSON_ID}/video`) },
  { method: "POST", pattern: re("progress") },
  { method: "POST", pattern: re(`modules/${MODULE_ID}/quiz/attempts`) },
  { method: "POST", pattern: re(`paths/${PATH_ID}/final-quiz/attempts`) },
  { method: "POST", pattern: re(`quiz/attempts/${UUID}/submit`) },
  { method: "GET", pattern: re("review/due") },
  { method: "POST", pattern: re(`review/${ITEM_ID}/result`) },
  { method: "POST", pattern: re(`modules/${MODULE_ID}/lab/check`) },
  { method: "GET", pattern: re("certificates") },
  { method: "POST", pattern: re(`certificates/${UUID}/code`) },
];

export function matchAcademyBff(method: string, subPath: string): AcademyBffRoute | null {
  const m = method.toUpperCase();
  return ACADEMY_BFF_ROUTES.find((r) => r.method === m && r.pattern.test(subPath)) ?? null;
}

/** Query parameters a call may carry (only `lang` of a lesson read); everything else is dropped, never forwarded. */
export function allowedAcademyQuery(subPath: string, search: URLSearchParams): URLSearchParams {
  const out = new URLSearchParams();
  if (re(`lessons/${LESSON_ID}`).test(subPath)) {
    const lang = search.get("lang");
    if (lang === "en" || lang === "vi") out.set("lang", lang);
  }
  return out;
}

// ── Page-only calls (as the staff console, lib/staffBff.ts review finding B1) ────────────────────────────────────
// The academy cookie is SameSite=Lax (the OAuth callback needs it) and lumiguides.it.com hosts are the SAME site
// (`it.com` is a public suffix), so a page of a sibling host could make the browser send it on an <img> or a form.
// Some academy calls act (enroll, progress, a quiz attempt, a new certificate code). So EVERY academy BFF call, GET
// included, must be the academy page's own fetch: it carries ACADEMY_CALL_HEADER (lib/academyClient.ts), which a
// navigation, an <img>, a <form> or another origin's page cannot send, and a browser that sends Sec-Fetch-Site must
// say same-origin.

export const ACADEMY_CALL_HEADER = "x-aa-academy";

export function academyCallRefusal(headers: { get(name: string): string | null }): "cross_site_refused" | "academy_call_required" | null {
  const site = headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return "cross_site_refused";
  if (headers.get(ACADEMY_CALL_HEADER) !== "1") return "academy_call_required";
  return null;
}

/** The upstream request headers: the academy token only. */
export function academyUpstreamHeaders(accessToken: string, requestId: string, json: boolean): Headers {
  const h = new Headers({ Accept: "application/json", Authorization: `Bearer ${accessToken}`, "X-Request-Id": requestId });
  if (json) h.set("Content-Type", "application/json");
  return h;
}
