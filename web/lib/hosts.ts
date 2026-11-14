// web/lib/hosts.ts
// AA-935 — host routing (card CRM-07 "host routing partner/traveller"; design v3 §2.1 "Hosts").
//
//   partner host (Dev partner-dev.)  the agency app: shell, sign-in, BFF, health
//   client host  (Dev go-dev., later) travellers and prospects: /c/* pages only (forms, proposals arrive with
//                                     AA-936 and later cards) and, since AA-979 P2b, the public trip page
//                                     /trip/<tourId> (moved from the CIS admin host); no agency route, no BFF, no
//                                     sign-in
//   academy host (Dev academy.)      AA-1105 (root ADR 0022 decisions 2 and 6): the AA Academy only: /academy/* (pages,
//                                     its own sign-in, its BFF), the public certificate verify page /verify/<code>, the
//                                     language switch and static files; "/" goes to /academy. Every agency, studio,
//                                     staff-console and client route answers 404 there, and /academy and /verify answer
//                                     404 on the partner and client hosts (local development serves them on localhost)
//   any other Host                   404 (the ALB already sends only the partner host here; this is the second line)
//
// Cookies are host-only (lib/session.ts), so the two hosts never share a session. Edge-safe: no Node API.

export type HostKind = "partner" | "client" | "academy" | "unknown";

/** Paths the client host serves. Everything else on it is 404. */
export const CLIENT_PATH_PREFIXES = [
  "/c/",
  "/trip/",
  "/_next/",
  "/healthz",
  "/favicon.ico",
  "/icon.svg",
  "/robots.txt",
] as const;

/** Pages that exist on the client host only: 404 on the partner host (local development serves both). */
export const CLIENT_ONLY_PREFIXES = ["/c/", "/trip/"] as const;

export function clientOnlyPath(pathname: string): boolean {
  return CLIENT_ONLY_PREFIXES.some((p) => pathname.startsWith(p));
}

/** Paths of the partner host reachable without a session. */
export const PUBLIC_PARTNER_PATHS = [
  "/auth/",
  "/healthz",
  "/health",
  "/signed-out",
  "/_next/",
  "/favicon.ico",
  // app/icon.svg and public/robots.txt: without them a signed-out visit (and every crawler) would start an OAuth
  // redirect chain just to load the favicon, and crawlers would never read the "Disallow: /" file.
  "/icon.svg",
  "/robots.txt",
  // AA-939: the invitation landing (the person has no account yet). /join/accept and /join/confirm need a session.
  "/join",
  "/join/invite",
  "/join/claim",
  // AA-1036 (R3 slice 1): the Content studio's public parts in the one agency app: its draft legal pages (outside the
  // gate, as in the studio app) and the brand logo they show (public/assets/).
  "/legal/partner-terms",
  "/legal/privacy",
  "/assets/",
  // AA-1008 (root ADR 0016 decision 9): the kit gallery of the per-PR visual run (no session there). It renders only on a
  // server started with KIT_GALLERY=1; a deployed build answers 404, so this opens nothing on Dev or production.
  "/kit-gallery",
] as const;

export function hostOnly(hostHeader: string | null): string {
  return (hostHeader ?? "").trim().toLowerCase();
}

export function classifyHost(
  hostHeader: string | null,
  cfg: { partnerHosts: string[]; clientHosts: string[]; academyHosts?: string[]; environment: string },
): HostKind {
  const host = hostOnly(hostHeader);
  if (!host) return "unknown";
  // AA-1105: checked first, so an academy host listed by mistake in another list still serves the academy only.
  if ((cfg.academyHosts ?? []).includes(host)) return "academy";
  if (cfg.partnerHosts.includes(host)) return "partner";
  if (cfg.clientHosts.includes(host)) return "client";
  const bare = host.replace(/:\d+$/, "");
  if ((cfg.environment === "local" || cfg.environment === "test") && (bare === "localhost" || bare === "127.0.0.1")) {
    return "partner";
  }
  return "unknown";
}

function matches(pathname: string, prefixes: readonly string[]): boolean {
  return prefixes.some((p) => (p.endsWith("/") ? pathname.startsWith(p) : pathname === p || pathname === `${p}/`));
}

export function clientPathAllowed(pathname: string): boolean {
  return matches(pathname, CLIENT_PATH_PREFIXES);
}

export function isPublicPartnerPath(pathname: string): boolean {
  return matches(pathname, PUBLIC_PARTNER_PATHS);
}

/** A same-origin path to return to after sign-in, or "/" (no scheme, no protocol-relative, no backslash tricks). */
export function safeNext(raw: string | null | undefined): string {
  if (!raw || raw.length > 512) return "/";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\") || raw.includes("\\")) return "/";
  if (/[\u0000-\u001f]/.test(raw)) return "/";
  if (raw.startsWith("/auth/") || raw.startsWith("/bff/")) return "/";
  return raw;
}

/**
 * One-shot marker on the path a page sends to /auth/refresh after the CRM API answered 401. The refresh returns to
 * that path with the marker; a second 401 there means the API refuses tokens this web accepts (for example a wrong
 * issuer or client id in the API's environment), so the page shows an error state instead of looping
 * page -> refresh -> page until the browser gives up (and calling the Cognito token endpoint each round).
 */
export const REFRESH_MARKER = "_rf";

export function withRefreshMarker(path: string): string {
  if (hasRefreshMarker(path)) return path;
  return `${path}${path.includes("?") ? "&" : "?"}${REFRESH_MARKER}=1`;
}

export function hasRefreshMarker(path: string): boolean {
  const q = path.indexOf("?");
  if (q < 0) return false;
  return new URLSearchParams(path.slice(q + 1)).get(REFRESH_MARKER) === "1";
}

// ── AA-1036 (R3 slice 1, root ADR 0019 d6): the Content studio inside the one agency app ──────────────────────────────

/** The studio's pages: /portal and below (the exchange of the partner session for a CIS studio token runs for these). */
export function isStudioPage(pathname: string): boolean {
  return pathname === "/portal" || pathname.startsWith("/portal/");
}

/** The studio's access pages (choose a studio / no studio): signed in, but outside the studio shell and its exchange. */
export function isStudioAccessPage(pathname: string): boolean {
  return pathname.startsWith("/portal-access/");
}

/** The studio's BFF (/api/*): route handlers answer for themselves (401 JSON, never a redirect). */
export function isStudioBff(pathname: string): boolean {
  return pathname.startsWith("/api/");
}

/** The retired people-facing API-key sign-in of the studio (root ADR 0019 d6): 308 to the partner sign-in. */
export const API_KEY_SIGN_IN_PATH = "/tenant-login";

export function isApiKeySignIn(pathname: string): boolean {
  return pathname === API_KEY_SIGN_IN_PATH || pathname.startsWith(`${API_KEY_SIGN_IN_PATH}/`);
}

/** The studio home: where a tenant lands (root ADR 0019 d7) and where the retired API-key sign-in now leads. */
export const STUDIO_HOME_PATH = "/portal/dashboard";

// ── AA-1105 (root ADR 0022): the AA Academy host ───────────────────────────────────────────────────────────────────

/** The academy's own prefix: pages, sign-in (/academy/auth), BFF (/academy/bff), captions (/academy/captions). */
export const ACADEMY_PREFIX = "/academy";
/** The public certificate verify page (learn.http.v1 CertificateCode.verify_path "/verify/<code>"). */
export const VERIFY_PREFIX = "/verify/";

/** Paths the academy host serves besides /academy/* and /verify/*: static files, liveness, the language switch. */
export const ACADEMY_EXTRA_PATHS = [
  "/_next/",
  "/healthz",
  "/favicon.ico",
  "/icon.svg",
  "/robots.txt",
  "/prefs/locale",
] as const;

/** Whether a path belongs to the academy (its pages or the public verify page): 404 on every other host. */
export function isAcademyPath(pathname: string): boolean {
  return pathname === ACADEMY_PREFIX || pathname.startsWith(`${ACADEMY_PREFIX}/`) || pathname.startsWith(VERIFY_PREFIX);
}

/** Whether the academy host serves a path at all (everything else there is 404). */
export function academyPathAllowed(pathname: string): boolean {
  return isAcademyPath(pathname) || matches(pathname, ACADEMY_EXTRA_PATHS);
}

/** Whether a request's Host may reach the academy's route handlers: the academy host (or localhost in development). */
export function academyHostAllowed(
  hostHeader: string | null,
  cfg: { partnerHosts: string[]; clientHosts: string[]; academyHosts?: string[]; environment: string },
): boolean {
  const kind = classifyHost(hostHeader, cfg);
  if (kind === "academy") return true;
  const localDev = cfg.environment === "local" || cfg.environment === "test";
  const bare = hostOnly(hostHeader).replace(/:\d+$/, "");
  return localDev && kind === "partner" && (bare === "localhost" || bare === "127.0.0.1");
}
