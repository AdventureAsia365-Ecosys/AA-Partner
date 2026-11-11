// web/next.config.ts
// AA-935 — the CRM web runs as a Next.js standalone server in its own container (ECS Fargate ARM64 service
// aa-crm-dev-web behind CloudFront partner-dev. -> ALB host rule, AA-CIS-Infra crm_web.tf; ADR 0010 decision 2), not
// on Vercel: one agency host for the API (/crm/*) and the web (everything else).
import type { NextConfig } from "next";
import path from "path";

// Security headers on every response (same set as the tenant portal, AA-885). The Content-Security-Policy is set per
// request by proxy.ts instead (its form-action must name the Cognito domain from the environment: the sign-out form
// is answered with a redirect to the managed-login logout page).
const SECURITY_HEADERS = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

// AA-1036 (R3 slice 1, root ADR 0019 d6) — the Content studio moved into this app (app/(studio)). Its old links keep
// working: the T-number slugs of AA-759 (the map mirrors ROUTES in app/_kit/local/studio/portalNav.tsx, guarded by
// api/tests/unit/test_studio_guards.py) and its draft legal pages, which move from /terms and /privacy to /legal/*
// (lib/studio/legal.ts: the CRM already owns /legal/terms). All 308; Next passes the query string through, so
// ?piece= / ?tour_id= / ?resume_request_id= deep links keep working. Redirects run before proxy.ts.
const STUDIO_REDIRECTS: { source: string; destination: string }[] = [
  { source: "/portal/t1-rewrite", destination: "/portal/tours" },
  { source: "/portal/t4-pool", destination: "/portal/tours/mine" },
  // AA-671 — the Tour overview page became the Marketplace view ?view=mine (Next appends the request's query).
  { source: "/portal/marketplace", destination: "/portal/tours?view=mine" },
  { source: "/portal/tours/marketplace", destination: "/portal/tours?view=mine" },
  { source: "/portal/t0-brand", destination: "/portal/brand" },
  { source: "/portal/t7-planning", destination: "/portal/create/social" },
  { source: "/portal/t8-angle-gate", destination: "/portal/create/write" },
  { source: "/portal/t10-review", destination: "/portal/content" },
  { source: "/portal/t11-publish", destination: "/portal/content/publish" },
  { source: "/portal/t11-publish/connection", destination: "/portal/account/integrations" },
  { source: "/portal/activity", destination: "/portal/account/activity" },
  { source: "/portal/billing", destination: "/portal/account/billing" },
  { source: "/portal/settings", destination: "/portal/account/settings" },
  // AA-1036 R3 slice 3 (root ADR 0019 d6): no tenant API keys, so no API page; a machine API for agencies is R8.
  { source: "/portal/api", destination: "/portal/account/settings" },
  { source: "/portal/account/api", destination: "/portal/account/settings" },
  // Keep in step with LEGACY_LEGAL_REDIRECTS in lib/studio/legal.ts (tests/studio/merge.test.ts compares them).
  { source: "/terms", destination: "/legal/partner-terms" },
  { source: "/privacy", destination: "/legal/privacy" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: { root: path.resolve(__dirname) },
  outputFileTracingRoot: path.resolve(__dirname),
  async redirects() {
    return STUDIO_REDIRECTS.map((r) => ({ ...r, permanent: true }));
  },
  async headers() {
    return [
      { source: "/(.*)", headers: SECURITY_HEADERS },
      // AA-943: the planning form's path carries the signed link's token: never send it on as a Referer.
      { source: "/c/s/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
      // AA-946: the proposal page and the traveller portal carry their signed link's token in the path too.
      { source: "/c/p/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
      { source: "/c/t/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
      // AA-1071: a sales partner's inquiry link and its page link carry their token in the path too.
      { source: "/c/r/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
      { source: "/c/a/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
    ];
  },
};

export default nextConfig;
