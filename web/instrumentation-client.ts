// web/instrumentation-client.ts
// AA-979 P2b — Sentry in the browser (moved with the Content studio, AA-1036). The DSN is a public build-time value
// (NEXT_PUBLIC_SENTRY_DSN, a Docker build arg from the `dev` variable WEB_PUBLIC_SENTRY_DSN in Deploy Web Dev; empty =
// off).
// Same rules as the server: no PII, no tracing, no replay.
import * as Sentry from "@sentry/nextjs";
import { scrubEvent } from "./lib/sentryScrub";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
if (dsn) {
  Sentry.init({
    dsn,
    environment: "dev",
    release: process.env.NEXT_PUBLIC_GIT_SHA || undefined,
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
