// web/lib/team.ts — shapes of the Team & roles API (AA-939, /crm/v1/team, /plan, /recovery) and small helpers the
// Team page and the recovery banner share. Edge-safe (no node imports).
import { fmt, t, type Lang, type MessageKey } from "./i18n";
import type { Role } from "./nav";

export interface TeamMember {
  member_id: string;
  role: Role;
  status: "active" | "invited" | "disabled";
  email: string | null;
  joined_at: string | null;
  open_trips: number;
  is_me: boolean;
}

export interface TeamInvitation {
  invitation_id: string;
  role: Role;
  status: "pending" | "claimed";
  email_masked: string;
  expires_at: string | null;
  created_at: string | null;
}

export interface Plan {
  plan: "free" | "core" | "pro";
  member_limit: number;
  allowed_features: string[];
  seats_used: number;
}

export interface TeamPage {
  members: TeamMember[];
  invitations: TeamInvitation[];
  plan: Plan;
}

export interface ReassignQueue {
  trips: { trip_id: string; title: string; stage: string; from_member_id: string }[];
  clients_by_member: Record<string, number>;
}

export interface Recovery {
  request_id: string;
  reason: "owner_left" | "owner_unreachable" | "no_owner";
  eligible_at: string | null;
  opened_at: string | null;
  candidate_member_id: string;
  is_candidate: boolean;
}

/** Each member's place among the members of its role, 1-based, in the order the Team API lists them (join time). */
export function roleSeq(members: Pick<TeamMember, "member_id" | "role">[]): Map<string, number> {
  const seen = new Map<string, number>();
  const out = new Map<string, number>();
  for (const m of members) {
    const n = (seen.get(m.role) ?? 0) + 1;
    seen.set(m.role, n);
    out.set(m.member_id, n);
  }
  return out;
}

/** How a member is named on the Team page and the forms' owner picker: the address they were invited with, else
 *  (AA-1129, S283 audit row 15) the role and its place ("Agent 2"), never a bare id. */
export function memberLabel(m: Pick<TeamMember, "email" | "member_id" | "role">, lang: Lang = "en", seq?: number): string {
  if (m.email) return m.email;
  const role = t(lang, `role.${m.role}` as MessageKey);
  return seq ? fmt(t(lang, "trip.memberSeq"), { role, n: seq }) : role;
}

/** Active members other than `exclude`, as reassign targets. */
export function reassignTargets(members: TeamMember[], exclude: string): TeamMember[] {
  return members.filter((m) => m.status === "active" && m.member_id !== exclude);
}

export function seatsLeft(plan: Plan): number {
  return Math.max(0, plan.member_limit - plan.seats_used);
}

/** API error codes the Team page words (anything else: the generic error). */
export const TEAM_ERRORS = [
  "last_owner",
  "member_limit_reached",
  "already_member",
  "invitation_exists",
  "invalid_email",
  "member_disabled",
  "mail_not_configured",
  "invitations_not_configured",
] as const;

export function teamError(detail: string): (typeof TEAM_ERRORS)[number] | null {
  return (TEAM_ERRORS as readonly string[]).includes(detail) ? (detail as (typeof TEAM_ERRORS)[number]) : null;
}
