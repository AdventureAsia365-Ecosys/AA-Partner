// web/lib/crmIdentity.ts — AA-1025 (S264 hotfix): the answer shape of GET /bff/identity (app/bff/identity/route.ts),
// the CRM sign-in the Content studio shows on the same agency host (portal/lib/crmIdentity.ts reads it).

/** Always answered with 200: the studio must never see a non-2xx (the browser logs it as a console error). */
export type CrmIdentityAnswer =
  | { kind: "signed_out" }
  | { kind: "no_agency" }
  | { kind: "agency"; display_name: string | null; member_role: string | null }
  | { kind: "unknown" };

const MAX_TEXT = 200;

function shortText(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim().slice(0, MAX_TEXT) : null;
}

/** Only the two fields the studio shows from the CRM API /me answer; ids, locale and anything else are dropped. */
export function pickIdentity(body: unknown): CrmIdentityAnswer {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return { kind: "unknown" };
  const me = body as { display_name?: unknown; member_role?: unknown };
  return { kind: "agency", display_name: shortText(me.display_name), member_role: shortText(me.member_role) };
}
