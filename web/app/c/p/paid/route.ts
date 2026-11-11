// web/app/c/p/paid/route.ts — AA-1072 (item crm-03): where the agency's payment gateway sends the client back after
// a hosted payment (the gateway's return URL; it appends its own signed fields to the query). Nothing is read from
// those fields: the gateway's server-to-server notice (the API's IPN) is the only proof of payment. The handler drops
// the query (so the gateway fields leave the address bar, the history and any Referer) and sends the client to their
// proposal page, which says the payment is being confirmed and reads itself again (components/proposals/ClientPayBox).
// A static segment, so /c/p/[token] never sees "paid" as a link token.
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export function GET() {
  return new NextResponse(null, {
    status: 303,
    headers: { Location: "/c/p?payment=returned", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" },
  });
}
