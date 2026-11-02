// portal/lib/crmIdentity.ts
// AA-1025 (S264): who is signed in, from the LIVE CRM session, for the studio's access pages and Account settings.
//
// The studio holds no identity of its own: the partner access token carries only `sub` (the partner pool signs in by
// e-mail, the address is not a token claim) and CIS memberships hold no e-mail. The CRM web on the same host does know
// the agency the person is signed in to: its BFF GET /bff/me (web/lib/bff.ts, allowed for every CRM role) answers the
// CRM API /me with the sealed session's token and agency. The browser calls it on its own origin (the agency host routes
// /bff/* to the web, root ADR 0010 §6), so no new configuration, token or cookie is needed. Elsewhere (Vercel, a local
// studio, an API-key session) the call fails and nothing is shown.
//
// The person's own e-mail is not shown: the CRM gives member e-mails only on the owner-only Team page.

export const CRM_ME_PATH = "/bff/me";

export type CrmRole = "owner" | "agent" | "assistant";

/** What the CRM session says: signed in to an agency, signed in without one, or unknown (show nothing). */
export type CrmIdentity = { kind: "agency"; agencyName: string; role: CrmRole | null } | { kind: "no_agency" } | null;

const ROLE_LABEL: Record<CrmRole, string> = { owner: "Owner", agent: "Agent", assistant: "Assistant" };
const MAX_NAME = 120;

/** The identity from the BFF answer (status + parsed JSON body). Anything unexpected is null. */
export function readCrmIdentity(status: number, body: unknown): CrmIdentity {
  if (status === 409 && (body as { detail?: unknown } | null)?.detail === "no_agency_selected") return { kind: "no_agency" };
  if (status !== 200 || body === null || typeof body !== "object") return null;
  const me = body as { display_name?: unknown; member_role?: unknown };
  const name = typeof me.display_name === "string" ? me.display_name.trim().slice(0, MAX_NAME) : "";
  if (!name) return null; // never fall back to the agency id: an id says nothing to the person
  const role = typeof me.member_role === "string" && me.member_role in ROLE_LABEL ? (me.member_role as CrmRole) : null;
  return { kind: "agency", agencyName: name, role };
}

/** The sentence shown to the person, or null when nothing is known. */
export function signedInText(identity: CrmIdentity): string | null {
  if (identity === null) return null;
  if (identity.kind === "no_agency") return "Signed in to AA Partner, with no agency selected.";
  return identity.role
    ? `Signed in to AA Partner as ${ROLE_LABEL[identity.role]} of ${identity.agencyName}.`
    : `Signed in to AA Partner for ${identity.agencyName}.`;
}

/** Asks the CRM web on this origin who is signed in. Never throws; null when it cannot tell. */
export async function fetchCrmIdentity(fetchImpl: typeof fetch = fetch): Promise<CrmIdentity> {
  try {
    const res = await fetchImpl(CRM_ME_PATH, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
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
