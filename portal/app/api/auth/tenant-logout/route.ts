// frontend/app/api/auth/tenant-logout/route.ts
// AA-427 — clears the 5 httpOnly tenant cookies server-side.
//
// Sidebar.tsx / PlaceholderTabs.tsx used to clear the session by looping
// `document.cookie = "<name>=; path=/; max-age=0"` over the 5 cookie names.
// That stops working once the cookies are httpOnly — client JS can no
// longer see or overwrite them at all (expiry included), so the browser
// would keep sending a "logged out" user's real tenant JWT on every request
// after clicking Logout. This route does the clearing server-side instead.
import { NextResponse } from "next/server";

const TENANT_COOKIES = [
  "cis_role",
  "cis_tenant_token",
  "cis_tenant_id",
  "cis_tenant_name",
  "cis_tenant_plan",
];

export async function POST() {
  const response = NextResponse.json({ ok: true });
  for (const name of TENANT_COOKIES) {
    response.cookies.set(name, "", { path: "/", maxAge: 0 });
  }
  return response;
}
