// portal/lib/config.ts
// AA-979 P2b — the Content studio's runtime configuration, read once from the environment (AA-CIS-Infra
// accounts/aa365/crm_portal.tf on Fargate; the Vercel project aa-cis-portal sets none of the P2b names, so a Vercel
// build behaves exactly like the P2a portal: API-key sign-in only). Server-only.
//
//   ENVIRONMENT             dev | stg | prod | test | local
//   PORTAL_HOSTS            comma list of the hosts the studio answers (Dev portal.); empty = any (Vercel)
//   PORTAL_PUBLIC_ORIGIN    https://<agency host>: redirects and the same-origin write guard use it, never the
//                           standalone server's bind address (critic C2). Empty = the request's origin (Vercel)
//   PORTAL_API_KEY_SIGNIN   "false" turns the tenant API-key sign-in off (P2b clean-up); anything else = on
//   WEB_SESSION_SECRET      the CRM web's session key (AA-CIS-Infra crm/dev/web session_secret): the studio READS the
//                           web's sealed session cookie on the same host (never writes it). Empty = no partner
//                           sign-in (Vercel)
//   CRM_WEB_SIGN_IN_PATH    the web route that renews the partner session or signs in (default /auth/refresh)
//   CRM_WEB_SIGN_OUT_PATH   the web's sign-out (default /auth/sign-out): one sign-out for both apps
//   CRM_SIGN_OUT_FORM_ORIGIN  https://<managed-login host> the web's sign-out redirects to; added to the CSP
//                           form-action (lib/csp.ts). Empty = forms to this origin only (Vercel)
//
// The type is named WebConfig on purpose: lib/session.ts is a byte copy of web/lib/session.ts and imports
// `type WebConfig` from "./config"; it reads only `secureCookies` and `sessionSecret`.

export type Environment = "dev" | "stg" | "prod" | "test" | "local";

export interface WebConfig {
  environment: Environment;
  hosts: string[];
  publicOrigin: string;
  apiKeySignIn: boolean;
  sessionSecret: string;
  signInPath: string;
  signOutPath: string;
  /** The managed-login origin the sign-out redirect lands on (CSP form-action); empty on Vercel. */
  signOutFormOrigin: string;
  /** The partner (CRM web) session can be read: the session key is configured. */
  partnerSignIn: boolean;
  /** Cookies of the CRM web carry Secure and the __Host- / __Secure- prefixes (false only on http://localhost). */
  secureCookies: boolean;
}

const ENVIRONMENTS: readonly Environment[] = ["dev", "stg", "prod", "test", "local"];
const LOCAL: readonly Environment[] = ["test", "local"];
const MIN_SECRET = 32;

type Env = Record<string, string | undefined>;

function text(env: Env, name: string): string {
  return (env[name] ?? "").trim();
}

function origin(raw: string, allowHttp: boolean): string {
  if (!raw) return "";
  try {
    const url = new URL(raw);
    const ok = url.protocol === "https:" || (allowHttp && url.protocol === "http:");
    if (!ok || url.pathname !== "/" || url.search || url.hash || url.username || url.password) return "";
    return url.origin;
  } catch {
    return "";
  }
}

function path(raw: string, fallback: string): string {
  return /^\/[A-Za-z0-9/_-]{0,64}$/.test(raw) ? raw : fallback;
}

export function loadConfig(env: Env = process.env): WebConfig {
  const raw = text(env, "ENVIRONMENT").toLowerCase() || "dev";
  const environment = (ENVIRONMENTS as readonly string[]).includes(raw) ? (raw as Environment) : "dev";
  const local = LOCAL.includes(environment);
  const publicOrigin = origin(text(env, "PORTAL_PUBLIC_ORIGIN"), local);
  const sessionSecret = text(env, "WEB_SESSION_SECRET");
  return {
    environment,
    hosts: text(env, "PORTAL_HOSTS")
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
    publicOrigin,
    apiKeySignIn: text(env, "PORTAL_API_KEY_SIGNIN").toLowerCase() !== "false",
    sessionSecret,
    signInPath: path(text(env, "CRM_WEB_SIGN_IN_PATH"), "/auth/refresh"),
    signOutPath: path(text(env, "CRM_WEB_SIGN_OUT_PATH"), "/auth/sign-out"),
    signOutFormOrigin: origin(text(env, "CRM_SIGN_OUT_FORM_ORIGIN"), false),
    partnerSignIn: sessionSecret.length >= MIN_SECRET,
    secureCookies: !(local && publicOrigin.startsWith("http://")),
  };
}

let cached: WebConfig | null = null;

/** The process configuration (read once). */
export function config(): WebConfig {
  if (cached === null) cached = loadConfig();
  return cached;
}

/** Tests only: replace (or with null, forget) the cached configuration. */
export function setConfigForTests(value: WebConfig | null): void {
  cached = value;
}

/** True when the Host may be served (no PORTAL_HOSTS = any Host, the Vercel case). */
export function hostAllowed(cfg: Pick<WebConfig, "hosts">, hostHeader: string | null): boolean {
  if (cfg.hosts.length === 0) return true;
  return cfg.hosts.includes((hostHeader ?? "").trim().toLowerCase());
}
