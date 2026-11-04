// web/lib/studio/signOut.ts
// AA-979 P2b — the studio shell's sign-out (client side). The studio cookies are httpOnly, so the BFF clears them, then
// this POSTs the app's sign-out as a form (same origin): one sign-out for the studio, the CRM and Cognito.
// AA-1036 (root ADR 0019 d6): there is one sign-in, so there is no API-key page to fall back to; the app's sign-out
// is the way out even when the BFF cannot be reached.

const SIGN_OUT_PATH = "/auth/sign-out";

export async function signOutEverywhere(): Promise<void> {
  let next = SIGN_OUT_PATH;
  try {
    const res = await fetch("/api/auth/tenant-logout", { method: "POST" });
    const body = (await res.json()) as { sign_out?: unknown };
    if (typeof body.sign_out === "string" && body.sign_out.startsWith("/") && !body.sign_out.startsWith("//")) next = body.sign_out;
  } catch {
    /* sign out regardless: /auth/sign-out clears the studio cookies too */
  }
  const form = document.createElement("form");
  form.method = "post";
  form.action = next;
  document.body.appendChild(form);
  form.submit();
}
