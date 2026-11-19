// web/lib/people.ts
// AA-1129 (S283 UI audit rows 10 and 15, S285 live check): ONE rule for naming an agency member anywhere in the CRM web
// (menus, filters, the forms' owner picker, the Team page, reports, the access log). A member is named
//   1. by its name: "lan.nguyen · Agent" (the part of the invitation address before the "@", as the members API's
//      `display_name`; never the full address),
//   2. else by its place in its role: "Agent 2",
//   3. else by its role alone: "Agent";
// and a member the agency no longer has reads "Former member". Never an e-mail address, never an id. (The planned kit
// PersonLabel follows the same order.) Edge-safe, unit-tested (tests/people.test.ts).

import { fmt, t, type Lang, type MessageKey } from "./i18n";

export type MemberRole = "owner" | "agent" | "assistant";

export interface PersonRef {
  role: MemberRole;
  /** The name the members API gives (`display_name`), or {@link nameFromEmail} of an address the page already has. */
  display_name?: string | null;
  /** The member's place among the members of its role, 1-based, by join time. */
  seq?: number | null;
}

/** Longest name shown, as the API's `pipeline.display_name` (64 characters). */
const NAME_MAX = 64;

/** The part of an address before the "@" ("lan.nguyen"), trimmed and cut like the API's members `display_name`;
 *  null for nothing. Never returns the full address. */
export function nameFromEmail(email: string | null | undefined): string | null {
  const local = (email ?? "").split("@", 1)[0].trim();
  return local ? local.slice(0, NAME_MAX) : null;
}

/** "lan.nguyen · Agent", else "Agent 2", else "Agent"; "Former member" when the member is unknown. */
export function personLabel(lang: Lang, p: PersonRef | null | undefined): string {
  if (!p) return t(lang, "reports.formerMember");
  const role = t(lang, `role.${p.role}` as MessageKey);
  const name = (p.display_name ?? "").trim();
  // A name that still looks like an address (an older API) is cut to its local part, so no address ever shows.
  if (name) return fmt(t(lang, "trip.memberNamed"), { name: nameFromEmail(name) ?? name, role });
  if (p.seq && p.seq > 0) return fmt(t(lang, "trip.memberSeq"), { role, n: p.seq });
  return role;
}
