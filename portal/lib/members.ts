// portal/lib/members.ts
// AA-978 follow-up — Account › Members: the pure rules of the page (no React, no imports; `npm test` runs
// lib/members.test.ts). The shape is CIS GET /v1/account/members (services/tenant_membership/portal.py): the agency's
// invited + active Content studio memberships from the CIS 276 directory, a short identity reference, no name or
// e-mail (the directory has neither).

export type MemberRole = "owner" | "editor" | "viewer";
export type MemberStatus = "invited" | "active";

export interface AccountMember {
  membership_id: string;
  member_ref: string | null;
  role: MemberRole;
  status: MemberStatus;
  added_at: string;
  updated_at: string;
}

export interface AccountMembers {
  data: AccountMember[];
  total: number;
  active: number;
  invited: number;
}

export const ROLE_LABEL: Record<MemberRole, string> = { owner: "Owner", editor: "Editor", viewer: "Viewer" };

export const ROLE_HINT: Record<MemberRole, string> = {
  owner: "Manages the workspace, brand and connections",
  editor: "Writes, edits and publishes content",
  viewer: "Reads content and reports",
};

export const STATUS_LABEL: Record<MemberStatus, string> = { active: "Active", invited: "Invitation sent" };

/** The member's line: the short reference (12 hex of the sign-in identity, never the identity itself). */
export function memberLabel(m: Pick<AccountMember, "member_ref" | "status">): string {
  if (m.status === "invited" || !m.member_ref) return "Invited (not signed in yet)";
  return `Member ${m.member_ref.slice(0, 4)}·${m.member_ref.slice(4, 8)}·${m.member_ref.slice(8, 12)}`;
}

/** "2 active members · 1 invitation" (singular / plural), or "No members yet". */
export function summaryLine(b: Pick<AccountMembers, "active" | "invited">): string {
  if (b.active === 0 && b.invited === 0) return "No members yet";
  const parts = [`${b.active} active member${b.active === 1 ? "" : "s"}`];
  if (b.invited > 0) parts.push(`${b.invited} invitation${b.invited === 1 ? "" : "s"} pending`);
  return parts.join(" · ");
}

/** True when the workspace has no active owner (only AA can fix that today: recovery through support). */
export function missingOwner(rows: AccountMember[]): boolean {
  return rows.length > 0 && !rows.some((m) => m.role === "owner" && m.status === "active");
}
