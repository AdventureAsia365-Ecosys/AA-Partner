// web/lib/config.ts
// AA-935 — the CRM web's configuration, read once from the environment of the ECS task (AA-CIS-Infra
// accounts/aa365/crm_web.tf). Server-only: nothing here is ever sent to the browser.
//
// The repo is public (DM-6.10): no pool id, client id, account id or host of a real environment is a code constant.
// Every value comes from the environment; an incomplete identity configuration turns sign-in off (503) instead of
// guessing, while /healthz keeps answering so the target group stays healthy.
//
//   WEB_PUBLIC_ORIGIN      https://<partner host>              the origin users see (callback / sign-out URLs)
//   WEB_PARTNER_HOSTS      comma list of agency hosts           host routing (lib/hosts.ts); default = the origin's host
//   WEB_CLIENT_HOSTS       comma list of traveller hosts        client pages only (go-dev., later); may be empty
//   CRM_API_ORIGIN         https://<partner host>              where the BFF reaches /crm/v1 (server to server)
//   COGNITO_DOMAIN         https://<managed login domain>      EXTERNAL pool until AA-939, then the partner pool
//   COGNITO_CLIENT_ID      the app client (cis-portal on Dev until AA-939, design v3 Fix C10)
//   COGNITO_CLIENT_SECRET  that client's secret (Secrets Manager, never in the image)
//   WEB_SESSION_SECRET     >= 32 random characters: seals the session cookies (Secrets Manager)
//   CONTENT_STUDIO_URL     the tenant portal (Content studio) the sidebar links to (cross-link, Fix C10)
//   ENVIRONMENT            dev | stg | prod | test | local
//   APP_VERSION, GIT_SHA   baked into the image, echoed by /healthz

export type Environment = "dev" | "stg" | "prod" | "test" | "local";

export interface WebConfig {
  environment: Environment;
  publicOrigin: string;
  partnerHosts: string[];
  clientHosts: string[];
  crmApiOrigin: string;
  cognitoDomain: string;
  cognitoClientId: string;
  cognitoClientSecret: string;
  sessionSecret: string;
  contentStudioUrl: string;
  version: string;
  gitSha: string;
  /** Every identity value is present and valid: sign-in can run. */
  authConfigured: boolean;
  /** What is missing or wrong (names only, never a value). */
  problems: string[];
  /** Cookies carry Secure and the __Host- / __Secure- prefixes (false only for http://localhost development). */
  secureCookies: boolean;
}

const ENVIRONMENTS: readonly Environment[] = ["dev", "stg", "prod", "test", "local"];
const LOCAL: readonly Environment[] = ["test", "local"];
const MIN_SECRET = 32;

type Env = Record<string, string | undefined>;

function text(env: Env, name: string): string {
  return (env[name] ?? "").trim();
}

function origin(raw: string, allowHttp: boolean): string | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  const ok = url.protocol === "https:" || (allowHttp && url.protocol === "http:");
  if (!ok || url.pathname !== "/" || url.search || url.hash || url.username || url.password) return null;
  return url.origin;
}

function hosts(raw: string): string[] {
  return raw
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
}

export function loadConfig(env: Env = process.env): WebConfig {
  const rawEnv = text(env, "ENVIRONMENT").toLowerCase() || "dev";
  const environment = (ENVIRONMENTS as readonly string[]).includes(rawEnv) ? (rawEnv as Environment) : "dev";
  const local = LOCAL.includes(environment);
  const problems: string[] = [];
  if (rawEnv !== environment) problems.push("ENVIRONMENT");

  const publicOrigin = origin(text(env, "WEB_PUBLIC_ORIGIN"), local) ?? "";
  if (!publicOrigin) problems.push("WEB_PUBLIC_ORIGIN");
  const crmApiOrigin = origin(text(env, "CRM_API_ORIGIN"), local) ?? "";
  if (!crmApiOrigin) problems.push("CRM_API_ORIGIN");
  const cognitoDomain = origin(text(env, "COGNITO_DOMAIN"), false) ?? "";
  if (!cognitoDomain) problems.push("COGNITO_DOMAIN");
  const cognitoClientId = text(env, "COGNITO_CLIENT_ID");
  if (!/^[A-Za-z0-9]{1,128}$/.test(cognitoClientId)) problems.push("COGNITO_CLIENT_ID");
  const cognitoClientSecret = text(env, "COGNITO_CLIENT_SECRET");
  if (!cognitoClientSecret) problems.push("COGNITO_CLIENT_SECRET");
  const sessionSecret = text(env, "WEB_SESSION_SECRET");
  if (sessionSecret.length < MIN_SECRET) problems.push("WEB_SESSION_SECRET");

  const partnerHosts = hosts(text(env, "WEB_PARTNER_HOSTS"));
  if (partnerHosts.length === 0 && publicOrigin) partnerHosts.push(new URL(publicOrigin).host);
  const clientHosts = hosts(text(env, "WEB_CLIENT_HOSTS")).filter((h) => !partnerHosts.includes(h));
  const contentStudioUrl = origin(text(env, "CONTENT_STUDIO_URL"), local) ?? "";

  const secureCookies = !(local && publicOrigin.startsWith("http://"));
  return {
    environment,
    publicOrigin,
    partnerHosts,
    clientHosts,
    crmApiOrigin,
    cognitoDomain,
    cognitoClientId,
    cognitoClientSecret,
    sessionSecret,
    contentStudioUrl,
    version: text(env, "APP_VERSION") || "unknown",
    gitSha: text(env, "GIT_SHA") || "unknown",
    authConfigured: problems.length === 0,
    problems,
    secureCookies,
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
