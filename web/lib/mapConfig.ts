// web/lib/mapConfig.ts
// AA-948 (card CRM-20) — the public Mapbox token of the proposal builder's map, read from the ECS task environment at
// request time (server only; the page passes it to the client map). Runtime config, never a build arg and never in the
// repo: the token is a URL-restricted public token (pk.) of the agency host, mapped into the task from a key of the web
// secret by AA-CIS-Infra (docs/implementation-notes/AA-948.md, "Hand step: the Mapbox token").
//
//   MAPBOX_PUBLIC_TOKEN   pk.<...>   empty or missing = no map: the builder shows its "map not set up" state
//
// A secret token (sk.) or anything that is not a public token is refused here, so a wrong value can never reach the
// browser: the builder then shows the same empty state.

export const MAPBOX_TOKEN_ENV = "MAPBOX_PUBLIC_TOKEN";

const PUBLIC_TOKEN = /^pk\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;

type Env = Record<string, string | undefined>;

/** The builder map's public token, or "" when none (or not a public token) is configured. */
export function builderMapToken(env: Env = process.env): string {
  const raw = (env[MAPBOX_TOKEN_ENV] ?? "").trim();
  return PUBLIC_TOKEN.test(raw) ? raw : "";
}
