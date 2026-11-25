// web/lib/securityHeaders.ts
// AA-1111 (S284, ZAP baseline triage of S283) — the CRM web's security headers in one place (the policy the CIS and
// Booking admins send, plus this app's form-action and frame-ancestors per host and page).
//
// - STATIC_SECURITY_HEADERS: sent on every response by next.config.ts headers() (AA-885 set + COOP same-origin).
// - contentSecurityPolicy(): the full Content-Security-Policy. proxy.ts sends it on every response with a fresh nonce
//   per request (script-src 'nonce-…' 'strict-dynamic', no 'unsafe-inline' for scripts); Next reads the nonce from the
//   request's CSP header for its own scripts, app/layout.tsx puts it on the restore scripts. The paths the proxy skips
//   (build chunks, image optimiser, favicon, the mail import upload) get it without a nonce from next.config.ts.
// - style-src keeps 'unsafe-inline': the kit and the pages use React inline styles everywhere (a nonce cannot cover
//   style attributes); moving them to classes is a kit-wide change.
// - connect-src names the Mapbox GL hosts (AA-948, the proposal builder's map: its API, tiles and telemetry; the same
//   list as the TripPlanner planner) beside 'self' and the Sentry ingest; workers come from blob: (worker-src).
// - img-src / media-src allow any https: source (tour photos, agency logos, academy videos on presigned S3 URLs);
//   every other fetch directive is 'self' plus the Google Fonts and Sentry origins the browser really talks to.
//
// Dependency-free (no imports): next.config.ts and proxy.ts both use it.

export const NONCE_HEADER = "x-nonce";

export const HSTS_VALUE = "max-age=63072000; includeSubDomains";

/** The headers that do not change per request (next.config.ts, every path). */
export const STATIC_SECURITY_HEADERS: { key: string; value: string }[] = [
  { key: "Strict-Transport-Security", value: HSTS_VALUE },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

const GOOGLE_FONTS_CSS = "https://fonts.googleapis.com";
const GOOGLE_FONTS_FILES = "https://fonts.gstatic.com";
/** AA-948: the hosts Mapbox GL talks to (style and tiles, the tile CDN, telemetry). */
export const MAPBOX_HOSTS = ["https://api.mapbox.com", "https://*.tiles.mapbox.com", "https://events.mapbox.com"];

/** The origin of a Sentry DSN (where the browser SDK posts events), or null when there is no valid DSN. */
export function sentryOrigin(dsn: string | undefined | null): string | null {
  const raw = (dsn ?? "").trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" ? url.origin : null;
  } catch {
    return null;
  }
}

/** A fresh base64 nonce (128 bits) for one response. */
export function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export interface CspOptions {
  /** Per-request nonce; without one scripts are 'self' only (paths that never serve an inline script). */
  nonce?: string;
  /** `next dev` needs eval for its refresh runtime; never set on a build. */
  dev?: boolean;
  /** Extra connect-src origins (the Sentry ingest origin). */
  connectSrc?: (string | null | undefined)[];
  /** Extra form-action origins (the managed-login domains a sign-out form is redirected to). */
  formAction?: (string | null | undefined)[];
  /** frame-ancestors sources; default 'none' (an inquiry form page names its allowed origins). */
  frameAncestors?: string;
}

const present = (list: (string | null | undefined)[] | undefined) => (list ?? []).filter((o): o is string => Boolean(o));

export function contentSecurityPolicy(opts: CspOptions = {}): string {
  const script = opts.nonce ? ["'self'", `'nonce-${opts.nonce}'`, "'strict-dynamic'"] : ["'self'"];
  if (opts.dev) script.push("'unsafe-eval'");
  // frame-ancestors first: the CI standalone smoke (ci.yml "Web Build") greps the header for that prefix.
  const directives: [string, string[]][] = [
    ["frame-ancestors", [opts.frameAncestors || "'none'"]],
    ["default-src", ["'self'"]],
    ["script-src", script],
    ["style-src", ["'self'", "'unsafe-inline'", GOOGLE_FONTS_CSS]],
    ["font-src", ["'self'", "data:", GOOGLE_FONTS_FILES]],
    ["img-src", ["'self'", "data:", "blob:", "https:"]],
    ["media-src", ["'self'", "blob:", "https:"]],
    ["connect-src", ["'self'", ...MAPBOX_HOSTS, ...present(opts.connectSrc)]],
    ["worker-src", ["'self'", "blob:"]],
    ["frame-src", ["'self'"]],
    ["manifest-src", ["'self'"]],
    ["base-uri", ["'self'"]],
    ["form-action", ["'self'", ...present(opts.formAction)]],
    ["object-src", ["'none'"]],
  ];
  return directives.map(([name, sources]) => `${name} ${sources.join(" ")}`).join("; ");
}
