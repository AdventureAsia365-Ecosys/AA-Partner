// portal/instrumentation.ts
// AA-979 P2b — Sentry on the Content studio's server (Fargate). On only when SENTRY_DSN is set (the task's secret
// crm/<env>/portal-sentry-dsn, mapped with AA-CIS-Infra crm_portal_sentry_enabled); no DSN = nothing is sent (Vercel,
// local). environment = ENVIRONMENT, release = the image's git SHA; no PII (lib/sentryScrub.ts), no tracing.
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
