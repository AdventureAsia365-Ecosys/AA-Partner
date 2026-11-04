// web/lib/studio/crmIdentity.ts
// AA-1025 (S264): who is signed in, from the LIVE CRM session, for the studio's access pages and Account settings.
//
// The studio holds no identity of its own: the partner access token carries only `sub` (the partner pool signs in by
// e-mail, the address is not a token claim) and CIS memberships hold no e-mail. The CRM web on the same host does know
// the agency the person is signed in to: its GET /bff/identity (web/app/bff/identity/route.ts) asks the CRM API /me
// with the sealed session's token and agency and answers ALWAYS 200 JSON {kind: "signed_out" | "no_agency" | "unknown"}
// or {kind: "agency", display_name, member_role}. Not /bff/me: that answers 409 / 401 for a person without an agency
// or a session, and the browser logs every non-2xx fetch as a console error on the studio page (S264 hotfix). The
// browser calls it on its own origin (the agency host routes /bff/* to the web, root ADR 0010 §6), so no new
// configuration, token or cookie is needed. Elsewhere (a local studio, an API-key session) the call fails or answers
// something else, and nothing is shown.
//
// AA-1036 (an AA-1007 leftover): the person's OWN sign-in address is shown ("Signed in as …"): the answer's `email`
// comes from the agency app's sealed session (the id token at sign-in), never from another member's record.

export const CRM_IDENTITY_PATH = "/bff/identity";

export type CrmRole = "owner" | "agent" | "assistant";

/** What the CRM session says: signed in to an agency, signed in without one, or unknown (show nothing). */
export type CrmIdentity =
  | { kind: "agency"; agencyName: string; role: CrmRole | null; email: string | null }
  | { kind: "no_agency"; email: string | null }
  | null;

const ROLE_LABEL: Record<CrmRole, string> = { owner: "Owner", agent: "Agent", assistant: "Assistant" };
const MAX_NAME = 120;
const MAX_EMAIL = 254;

function emailOf(v: unknown): string | null {
  return typeof v === "string" && v.includes("@") && v.trim().length <= MAX_EMAIL ? v.trim() : null;
}

/**
 * The identity from the web's answer (status + parsed JSON body). Anything unexpected is null: signed_out, unknown, a
 * missing or unknown `kind`, a non-200. Tolerant on purpose (an older web answering /bff/me shapes also reads as null).
 */
export function readCrmIdentity(status: number, body: unknown): CrmIdentity {
  if (status !== 200 || body === null || typeof body !== "object") return null;
  const me = body as { kind?: unknown; display_name?: unknown; member_role?: unknown; email?: unknown };
  if (me.kind === "no_agency") return { kind: "no_agency", email: emailOf(me.email) };
  if (me.kind !== "agency") return null;
  const name = typeof me.display_name === "string" ? me.display_name.trim().slice(0, MAX_NAME) : "";
  if (!name) return null; // never fall back to the agency id: an id says nothing to the person
  const role = typeof me.member_role === "string" && Object.hasOwn(ROLE_LABEL, me.member_role) ? (me.member_role as CrmRole) : null;
  return { kind: "agency", agencyName: name, role, email: emailOf(me.email) };
}

/** The sentence shown to the person, or null when nothing is known. */
export function signedInText(identity: CrmIdentity): string | null {
  if (identity === null) return null;
  const who = identity.email ? ` as ${identity.email}` : "";
  if (identity.kind === "no_agency") return `Signed in to AA Partner${who}, with no agency selected.`;
  if (identity.email) {
    return identity.role
      ? `Signed in to AA Partner as ${identity.email} (${ROLE_LABEL[identity.role]} of ${identity.agencyName}).`
      : `Signed in to AA Partner as ${identity.email}, for ${identity.agencyName}.`;
  }
  return identity.role
    ? `Signed in to AA Partner as ${ROLE_LABEL[identity.role]} of ${identity.agencyName}.`
    : `Signed in to AA Partner for ${identity.agencyName}.`;
}

/** Asks the CRM web on this origin who is signed in. Never throws; null when it cannot tell. */
export async function fetchCrmIdentity(fetchImpl: typeof fetch = fetch): Promise<CrmIdentity> {
  try {
    const res = await fetchImpl(CRM_IDENTITY_PATH, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return readCrmIdentity(res.status, body);
  } catch {
    return null;
  }
}
