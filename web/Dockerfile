# syntax=docker/dockerfile:1
# AA-Partner CRM web image (AA-935, card CRM-07). Next.js standalone server, run as a Fargate ARM64 task behind the
# shared ALB (target group port 3000, health check /healthz — AA-CIS-Infra accounts/aa365/crm_web.tf). Built natively
# on an arm64 runner (CI "Web Docker Build Check", Deploy Dev).
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund
COPY . .
# The build never needs a secret: every runtime value is read from the environment of the task (lib/config.ts).
RUN NODE_OPTIONS=--max-old-space-size=1536 npm run build

FROM node:22-alpine AS runtime
ARG GIT_SHA=unknown
# Release version: CI and the deploy read it from .release-please-manifest.json (outside this build context).
ARG APP_VERSION=unknown
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    GIT_SHA=${GIT_SHA} \
    APP_VERSION=${APP_VERSION} \
    # Keep-alive must outlive the ALB idle timeout (100 s); Node's 5 s default makes the ALB reuse a closed
    # connection and answer 502 (CIS lesson S231, AA-741). Read by the Next standalone server.
    KEEP_ALIVE_TIMEOUT=105000
# The server needs node only: drop npm, npx and corepack from the runtime image (smaller attack surface, and their
# bundled packages are what image scanners flag most).
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
# Fargate sets HOSTNAME to the task's own hostname at run time, overriding the ENV above, and the Next standalone
# server binds to HOSTNAME: it then listens on the private IP only, the 127.0.0.1 HEALTHCHECK fails and ECS replaces
# the task every few minutes (S249). Force all interfaces at start, without a shell.
CMD ["node", "-e", "process.env.HOSTNAME='0.0.0.0'; require('./server.js')"]
