// web/app/c/a/[token]/route.ts — AA-1071 (crm-056): a sales partner's page link lands here (the Fix B11 model of the
// traveller portal, app/c/t/[token]/route.ts). The token is exchanged for the host-only cookie aa_partner_link
// (httpOnly, Secure, SameSite=Strict, scoped to the API's /crm/public/v1/partner routes) and the browser is sent to
// /c/a, so the token leaves the address bar, the history and any Referer. The partner still needs an e-mail code.
import { NextResponse } from "next/server";
import { partnerCookie } from "@/lib/partners";
import { CLIENT_TOKEN } from "@/lib/proposals";

export const dynamic = "force-dynamic";

export async function GET(_request: Request, { params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) {
    return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
  }
  const res = new NextResponse(null, { status: 303, headers: { Location: "/c/a", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" } });
  res.headers.append("Set-Cookie", partnerCookie(token));
  return res;
}
