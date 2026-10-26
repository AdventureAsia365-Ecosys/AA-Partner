// portal/lib/signOut.ts
// AA-979 P2b — the studio shell's sign-out (client side). The studio cookies are httpOnly, so the BFF clears them;
// when the person signed in with the partner pool (the CRM web's session on the same host), the BFF answers the web's
// sign-out path and this POSTs it as a form (same origin): one sign-out for the studio, the CRM and Cognito.
// Otherwise (an API-key session, or Vercel) it goes to the API-key sign-in page, as before.

export async function signOutEverywhere(): Promise<void> {
  let next: string | null = null;
  try {
    const res = await fetch("/api/auth/tenant-logout", { method: "POST" });
    const body = (await res.json()) as { sign_out?: unknown };
    next = typeof body.sign_out === "string" && body.sign_out.startsWith("/") ? body.sign_out : null;
  } catch {
    /* sign out locally regardless */
  }
  if (!next) {
    window.location.href = "/tenant-login";
    return;
  }
  const form = document.createElement("form");
  form.method = "post";
  form.action = next;
  document.body.appendChild(form);
  form.submit();
}
