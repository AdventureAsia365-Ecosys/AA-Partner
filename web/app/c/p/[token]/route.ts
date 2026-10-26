// web/app/c/p/[token]/route.ts — the signed proposal link's landing (AA-950, card CRM-21b; design v3 §2.9 rule 1,
// Fix B11). The link the client gets stays /c/p/<token> (AA-946). This handler exchanges the token for the host-only
// cookie aa_proposal_link (httpOnly, Secure, SameSite=Strict, scoped to the API's /crm/public/v1/proposal routes) and
// redirects to /c/p, so the token leaves the address bar, the history and any Referer, and page script never sees it.
// Nothing binds the link to a device: the token stays valid until the link expires, so a second device (or the
// spouse) opening the same link gets the same exchange. The agent's "Re-issue link" revokes it. The web server never
// calls the API with the token (the browser does, on this origin, so the per-address limit keys on the visitor).
import { NextResponse } from "next/server";
import { proposalCookie } from "@/lib/clientProposal";
import { CLIENT_TOKEN } from "@/lib/proposals";

export const dynamic = "force-dynamic";

export async function GET(_request: Request, { params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) {
    return new NextResponse("Not Found", { status: 404, headers: { "Content-Type": "text/plain", "Cache-Control": "no-store" } });
  }
  // A relative Location: the client host the visitor used (CloudFront -> ALB host rule), never an internal origin.
  const res = new NextResponse(null, { status: 303, headers: { Location: "/c/p", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" } });
  res.headers.append("Set-Cookie", proposalCookie(token));
  return res;
}
