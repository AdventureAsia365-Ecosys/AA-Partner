// web/lib/studio/members.ts
// AA-978 follow-up — Account › Members: the pure rules of the page (no React, no imports; `npm test` runs
// lib/members.test.ts). The shape is CIS GET /v1/account/members (services/tenant_membership/portal.py): the agency's
// invited + active Content studio memberships from the CIS 276 directory, a short identity reference, no name or
// e-mail (the directory has neither). AA-1036 (R3): the words take the UI language (lib.messages.ts), English by default.

import { translator, type Lang } from "@/app/_kit/core/i18n";
import { MEMBERS_LIB_MESSAGES } from "./lib.messages";

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

const ROLE_KEY = { owner: "roleOwner", editor: "roleEditor", viewer: "roleViewer" } as const;
const HINT_KEY = { owner: "hintOwner", editor: "hintEditor", viewer: "hintViewer" } as const;
const STATUS_KEY = { active: "statusActive", invited: "statusInvited" } as const;

/** A role in words (an unknown role passes through). */
export function roleLabel(role: string, lang: Lang = "en"): string {
  return Object.hasOwn(ROLE_KEY, role) ? translator(MEMBERS_LIB_MESSAGES, lang)(ROLE_KEY[role as MemberRole]) : role;
}

/** What a role may do (empty for an unknown role). */
export function roleHint(role: string, lang: Lang = "en"): string {
  return Object.hasOwn(HINT_KEY, role) ? translator(MEMBERS_LIB_MESSAGES, lang)(HINT_KEY[role as MemberRole]) : "";
}

/** A membership status in words (an unknown status passes through). */
export function statusLabel(status: string, lang: Lang = "en"): string {
  return Object.hasOwn(STATUS_KEY, status)
    ? translator(MEMBERS_LIB_MESSAGES, lang)(STATUS_KEY[status as MemberStatus])
    : status;
}

/** The member's line: the short reference (12 hex of the sign-in identity, never the identity itself). */
export function memberLabel(m: Pick<AccountMember, "member_ref" | "status">, lang: Lang = "en"): string {
  const t = translator(MEMBERS_LIB_MESSAGES, lang);
  if (m.status === "invited" || !m.member_ref) return t("invited");
  return t("member", `${m.member_ref.slice(0, 4)}·${m.member_ref.slice(4, 8)}·${m.member_ref.slice(8, 12)}`);
}

/** "2 active members · 1 invitation" (singular / plural), or "No members yet". */
export function summaryLine(b: Pick<AccountMembers, "active" | "invited">, lang: Lang = "en"): string {
  const t = translator(MEMBERS_LIB_MESSAGES, lang);
  if (b.active === 0 && b.invited === 0) return t("none");
  const parts = [t("active", b.active)];
  if (b.invited > 0) parts.push(t("pending", b.invited));
  return parts.join(" · ");
}

/** True when the workspace has no active owner (only AA can fix that today: recovery through support). */
export function missingOwner(rows: AccountMember[]): boolean {
  return rows.length > 0 && !rows.some((m) => m.role === "owner" && m.status === "active");
}
