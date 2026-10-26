# syntax=docker/dockerfile:1
# AA-Partner Content studio image (portal/, AA-979 CRM-P2b). Next.js standalone server, run as the Fargate ARM64 task
# aa-crm-<env>-portal behind the shared ALB (target group port 3000, health check /healthz — AA-CIS-Infra
# accounts/aa365/crm_portal.tf). Built natively on an arm64 runner (CI "Portal Docker Build Check", Deploy Portal Dev).
# Same shape as web/Dockerfile, except: no .npmrc in portal/, and xlsx installs from cdn.sheetjs.com (package.json),
# so the build needs that egress (the SBOM lists it).
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 \
    # next.config.ts: standalone output + the /portal-assets asset prefix (the Vercel build sets neither).
    PORTAL_STANDALONE=1
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Public build-time values only (inlined into the browser bundle): the Sentry DSN of the browser SDK is public by
# design. Every server value is read from the task's environment at run time (lib/config.ts).
ARG NEXT_PUBLIC_SENTRY_DSN=""
ARG GIT_SHA=unknown
ENV NEXT_PUBLIC_SENTRY_DSN=${NEXT_PUBLIC_SENTRY_DSN} \
    NEXT_PUBLIC_GIT_SHA=${GIT_SHA}
RUN NODE_OPTIONS=--max-old-space-size=1536 npm run build

FROM node:22-alpine AS runtime
ARG GIT_SHA=unknown
ARG APP_VERSION=unknown
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    GIT_SHA=${GIT_SHA} \
    APP_VERSION=${APP_VERSION} \
    # Keep-alive must outlive the ALB idle timeout (100 s): Node's 5 s default makes the ALB reuse a closed connection
    # and answer 502 (CIS lesson S231). Read by the Next standalone server.
    KEEP_ALIVE_TIMEOUT=105000
# The server needs node only: drop npm, npx and corepack from the runtime image.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.next/standalone ./
COPY --from=build --chown=10001:10001 /app/.next/static ./.next/static
COPY --from=build --chown=10001:10001 /app/public ./public
USER 10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
# Fargate sets HOSTNAME to the task's own hostname at run time, overriding the ENV above, and the Next standalone server
# binds to HOSTNAME: it would listen on the private IP only and fail the 127.0.0.1 health check (S249). Force all
# interfaces at start, without a shell.
CMD ["node", "-e", "process.env.HOSTNAME='0.0.0.0'; require('./server.js')"]
