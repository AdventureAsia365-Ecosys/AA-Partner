// web/app/api/tenant/me/route.ts (AA-885: moved from the admin app; AA-1036: into the one agency app)
// AA-427 — "/me" for the Content studio: the display fields of the verified studio session (the token itself never
// reaches the response). The httpOnly studio cookies are invisible to page JavaScript, so the shell asks here.
import { NextRequest, NextResponse } from "next/server";
import { requireStudio, withRenewed } from "@/lib/studio/auth-server";

export async function GET(req: NextRequest) {
  const auth = await requireStudio(req);
  if (!auth.ok) return auth.response;

  return withRenewed(
    NextResponse.json({
      tenant_id: auth.tenantId,
      tenant_name: auth.name,
      plan_tier: auth.planTier,
      // Root ADR 0019 d6: the partner sign-in is the only one (the API-key session is gone); kept for the shell.
      sign_in: "partner",
    }),
    auth.renewed,
    auth.cfg,
  );
}
