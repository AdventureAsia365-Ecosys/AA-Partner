// portal/app/healthz/route.ts
// AA-979 P2b — liveness of the Content studio task (ALB target group and the container healthCheck). Any Host (the ALB
// health check sends the task IP); outside the proxy matcher. No dependency is called: a CIS outage must not take the
// studio's target out (the pages say so instead).
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(
    {
      ok: true,
      service: "aa-partner-portal",
      version: process.env.APP_VERSION || "unknown",
      sha: process.env.GIT_SHA || "unknown",
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
