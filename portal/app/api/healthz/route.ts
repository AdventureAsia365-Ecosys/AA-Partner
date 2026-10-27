// portal/app/api/healthz/route.ts
// AA-993 V3 — the same liveness body as /healthz (lib/health.ts), on a path the agency host routes to the studio: there
// the ALB sends /healthz to the CRM web and /api/* to the studio, so the post-deploy verify
// (.github/verify/crm-portal.rows) reads the deployed commit and version here. Public, no dependency, never cached;
// it goes through the proxy's Host guard like every other /api route.
import { NextResponse } from "next/server";
import { healthBody } from "@/lib/health";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(healthBody(), { headers: { "Cache-Control": "no-store" } });
}
