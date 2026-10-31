// web/lib/supportAccess.ts
// AA-971 (card CRM-13b) — the shape of Settings > Data and privacy > AA support access (api/app/support/grants.py,
// GET / POST /crm/v1/support-access, POST /crm/v1/support-access/revoke) and the small pure helpers the settings card
// and the shell banner use: which banner shows (break-glass red before support amber), the day choices, the wording key
// of an API refusal.

export type SessionKind = "support" | "breakglass";
export type SessionStatus = "active" | "ended" | "revoked" | "expired";

export interface SupportGrant {
  grant_id: string;
  show_identity: boolean;
  starts_at: string;
  expires_at: string;
  granted_by_member_id: string;
}

export interface SupportSession {
  session_id: string;
  kind: SessionKind;
  status: SessionStatus;
  staff_ref: string;
  ticket_ref: string;
  reason_code: string | null;
  show_identity: boolean;
  started_at: string;
  expires_at: string;
  ended_at: string | null;
  owner_notified: boolean;
}

export interface SupportAccess {
  grant: SupportGrant | null;
  active_sessions: SupportSession[];
  recent_sessions: SupportSession[];
  can_manage: boolean;
  days_min: number;
  days_max: number;
  days_default: number;
  /** The agency's IANA zone (S262 round 3): the banner and Settings print times in it, never UTC. */
  timezone?: string;
}

export type BannerState =
  | { kind: "breakglass"; until: string; reason: string | null }
  | { kind: "support_session"; until: string; showIdentity: boolean }
  | { kind: "support_grant"; until: string; showIdentity: boolean }
  | null;

/** The banner every member sees (design §2.3 "Support banner"): an emergency session first, then a support person
 * working now, then a window the owner allowed that nobody uses yet. */
export function bannerState(view: SupportAccess | null): BannerState {
  if (!view) return null;
  const glass = view.active_sessions.find((s) => s.kind === "breakglass");
  if (glass) return { kind: "breakglass", until: glass.expires_at, reason: glass.reason_code };
  const support = view.active_sessions.find((s) => s.kind === "support");
  if (support) return { kind: "support_session", until: support.expires_at, showIdentity: support.show_identity };
  if (view.grant) return { kind: "support_grant", until: view.grant.expires_at, showIdentity: view.grant.show_identity };
  return null;
}

/** The day choices of the grant form (1..7 from the API, default 1 = 24 hours). */
export function dayChoices(view: Pick<SupportAccess, "days_min" | "days_max">): number[] {
  const out: number[] = [];
  for (let d = Math.max(1, view.days_min); d <= Math.min(7, view.days_max); d += 1) out.push(d);
  return out;
}

const KNOWN_ERRORS = new Set(["support_grant_active", "no_support_grant", "role_forbidden"]);

export function supportErrorKey(detail: string | undefined): string {
  return detail && KNOWN_ERRORS.has(detail) ? `support.error.${detail}` : "support.error.generic";
}

export const BREAKGLASS_REASONS = [
  "security_incident",
  "data_corruption",
  "legal_request",
  "owner_unreachable",
  "service_outage",
] as const;

/** The wording key of a break-glass reason code (an unknown code shows the generic wording). */
export function reasonKey(code: string | null): string {
  return code && (BREAKGLASS_REASONS as readonly string[]).includes(code)
    ? `support.reason.${code}`
    : "support.reason.other";
}
