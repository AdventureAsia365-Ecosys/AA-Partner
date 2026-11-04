// web/app/api/healthz/route.ts
// AA-993 V3 — the studio's liveness on a path the agency host routes to the studio (/api/*): the post-deploy verify
// (.github/verify/crm-portal.rows) reads the deployed commit and version here. AA-1036 (R3 slice 1, root ADR 0019 d6):
// the studio is part of the agency app now, so this answers for the agency app (lib/studio/health.ts); once the
// cut-over sends /api/* here, the portal rows read this body. Public, no dependency, never cached.
import { NextResponse } from "next/server";
import { healthBody } from "@/lib/studio/health";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(healthBody(), { headers: { "Cache-Control": "no-store" } });
}
