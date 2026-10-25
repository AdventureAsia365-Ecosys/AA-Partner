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

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: { root: path.resolve(__dirname) },
  outputFileTracingRoot: path.resolve(__dirname),
  async headers() {
    return [
      { source: "/(.*)", headers: SECURITY_HEADERS },
      // AA-943: the planning form's path carries the signed link's token: never send it on as a Referer.
      { source: "/c/s/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
      // AA-946: the proposal page and the traveller portal carry their signed link's token in the path too.
      { source: "/c/p/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
      { source: "/c/t/:token*", headers: [{ key: "Referrer-Policy", value: "no-referrer" }] },
    ];
  },
};

export default nextConfig;
