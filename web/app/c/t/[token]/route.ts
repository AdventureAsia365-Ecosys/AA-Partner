// web/app/c/t/[token]/route.ts — the traveller portal link's landing (AA-953, card CRM-24a; design v3 §2.9 rule 1, the
// Fix B11 model of the proposal page). The link each traveller gets stays /c/t/<token> (AA-946). This handler exchanges
// the token for the host-only cookie aa_portal_link (httpOnly, Secure, SameSite=Strict, scoped to the API's
// /crm/public/v1/traveller routes) and redirects to /c/t, so the token leaves the address bar, the history and any
// Referer, and page script never sees it. Nothing binds the link to a device: the token stays valid until the link
// expires, so a second device opening the same link gets the same exchange (and still needs its own e-mail code).
// The agent's "New portal link" revokes it. The web server never calls the API with the token.
import { NextResponse } from "next/server";
import { portalCookie } from "@/lib/clientPortal";
import { CLIENT_TOKEN } from "@/lib/proposals";

export const dynamic = "force-dynamic";

export async function GET(_request: Request, { params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) {
    return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
  }
  // A relative Location: the client host the visitor used (CloudFront -> ALB host rule), never an internal origin.
  const res = new NextResponse(null, { status: 303, headers: { Location: "/c/t", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" } });
  res.headers.append("Set-Cookie", portalCookie(token));
  return res;
}
