// web/instrumentation.ts
// AA-979 P2b — Sentry on the server, moved from the Content studio app with the studio (AA-1036, R3 slice 1, root ADR
// 0019 d6): it now covers the whole agency app. On only when SENTRY_DSN is set (AA-1111: the aa-crm-dev-web task maps
// key sentry_dsn of the crm/<env>/web secret behind AA-CIS-Infra crm_web_sentry_enabled; Sentry project
// aa-partner-web); no DSN = nothing is sent. environment = ENVIRONMENT, release = the image's git SHA; no PII
// (lib/sentryScrub.ts), no tracing. The browser DSN is the build-time variable WEB_PUBLIC_SENTRY_DSN.
import * as Sentry from "@sentry/nextjs";
import { scrubEvent } from "./lib/sentryScrub";

export async function register() {
  const dsn = process.env.SENTRY_DSN;
  if (process.env.NEXT_RUNTIME !== "nodejs" || !dsn) return;
  Sentry.init({
    dsn,
    environment: process.env.ENVIRONMENT || "dev",
    release: process.env.GIT_SHA || undefined,
    // Sentry 11: no user fields, cookies, headers, query strings, bodies or frame variables (no PII, no tokens).
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: false,
      urlQueryParams: false,
      httpBodies: [],
      stackFrameVariables: false,
    },
    tracesSampleRate: 0,
    beforeSend: scrubEvent,
  });
}

// Errors of server components, route handlers and the proxy (a no-op while Sentry is not initialised).
export const onRequestError = Sentry.captureRequestError;
