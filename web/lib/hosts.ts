// web/lib/hosts.ts
// AA-935 — host routing (card CRM-07 "host routing partner/traveller"; design v3 §2.1 "Hosts").
//
//   partner host (Dev partner-dev.)  the agency app: shell, sign-in, BFF, health
//   client host  (Dev go-dev., later) travellers and prospects: /c/* pages only (forms, proposals arrive with
//                                     AA-936 and later cards); no agency route, no BFF, no sign-in
//   any other Host                   404 (the ALB already sends only the partner host here; this is the second line)
//
// Cookies are host-only (lib/session.ts), so the two hosts never share a session. Edge-safe: no Node API.

export type HostKind = "partner" | "client" | "unknown";

/** Paths the client host serves. Everything else on it is 404. */
export const CLIENT_PATH_PREFIXES = ["/c/", "/_next/", "/healthz", "/favicon.ico", "/icon.svg", "/robots.txt"] as const;

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
] as const;

export function hostOnly(hostHeader: string | null): string {
  return (hostHeader ?? "").trim().toLowerCase();
}

export function classifyHost(
  hostHeader: string | null,
  cfg: { partnerHosts: string[]; clientHosts: string[]; environment: string },
): HostKind {
  const host = hostOnly(hostHeader);
  if (!host) return "unknown";
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
