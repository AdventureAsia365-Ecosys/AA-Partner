// portal/app/healthz/route.ts
// AA-979 P2b — liveness of the Content studio task (ALB target group and the container healthCheck). Any Host (the ALB
// health check sends the task IP); outside the proxy matcher. No dependency is called: a CIS outage must not take the
// studio's target out (the pages say so instead). Body: lib/health.ts (also served at /api/healthz).
import { NextResponse } from "next/server";
import { healthBody } from "@/lib/health";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(healthBody(), { headers: { "Cache-Control": "no-store" } });
}
