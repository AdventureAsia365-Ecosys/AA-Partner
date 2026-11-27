// web/app/c/a/[token]/route.ts — AA-1071 (crm-056): a sales partner's page link lands here (the Fix B11 model of the
// traveller portal, app/c/t/[token]/route.ts). The token is exchanged for the host-only cookie __Secure-aa_partner_link
// (httpOnly, Secure, SameSite=Strict, scoped to the API's /crm/public/v1/partner routes) and the browser is sent to
// /c/a, so the token leaves the address bar, the history and any Referer. The partner still needs an e-mail code.
import { NextResponse } from "next/server";
import { PARTNER_API, partnerCookie } from "@/lib/partners";
import { CLIENT_TOKEN } from "@/lib/proposals";

export const dynamic = "force-dynamic";

export async function GET(_request: Request, { params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) {
    return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
  }
  const res = new NextResponse(null, { status: 303, headers: { Location: "/c/a", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" } });
  res.headers.append("Set-Cookie", partnerCookie(token));
  // The pre-rename cookie (no __Secure- prefix) is no longer read; clear it so a still-valid link token does not sit
  // in the browser until it expires.
  // TODO(AA-1111): remove 30 days after this ships (the longest life of an old link cookie).
  res.headers.append("Set-Cookie", `aa_partner_link=; Path=${PARTNER_API}; Max-Age=0; HttpOnly; Secure; SameSite=Strict`);
  return res;
}
