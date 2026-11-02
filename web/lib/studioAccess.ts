// web/lib/studioAccess.ts
// AA-1025 (S264) — may the signed-in person open the Content studio for the agency they are in? The sidebar's "Open
// Content studio" link showed for every agency, and an agency without a studio tenant (or a person without a studio
// membership in it) landed on the studio's "No Content studio access" page, or worse, on a studio left over from
// another sign-in on the same browser (fixed in portal/proxy.ts).
//
// The source is the one the studio itself uses: CIS GET /auth/tenant-memberships with the person's partner access
// token (the same token the studio exchanges; the CIS tenant id of a studio is the CRM agency id, the membership feed
// relies on that too). The link shows when the current agency is among the person's ACTIVE studio memberships.
//
//   "yes"     the agency is one of the person's studios -> show the link
//   "no"      CIS answered and it is not               -> hide it
//   "unknown" no CIS URL, no agency, CIS refused (throttle, outage) -> keep the link (the studio says what is wrong)
//
// CIS throttles this route per person together with the studio's sign-in exchange (30 a minute), and the CRM layout
// renders on every page, so answers are memoised per (person, agency): 5 minutes for an answer, 30 s for "unknown".
// The memo holds only a yes / no per hash key, never a token or a tenant list.

import { createHash } from "node:crypto";
import type { Session } from "./session";

export type StudioAccess = "yes" | "no" | "unknown";

const TIMEOUT_MS = 2500;
export const ANSWER_TTL_MS = 5 * 60_000;
export const UNKNOWN_TTL_MS = 30_000;
const MEMO_MAX = 2000;

const memo = new Map<string, { until: number; value: StudioAccess }>();

/** Test hook: forget every memoised answer. */
export function resetStudioAccessMemo(): void {
  memo.clear();
}

function memoKey(session: Pick<Session, "iss" | "sub" | "tid">): string {
  return createHash("sha256").update(`${session.iss}|${session.sub}|${session.tid ?? ""}`).digest("hex").slice(0, 32);
}

/** The pure decision from CIS's answer (null = no usable answer). */
export function decide(memberships: { tenant_id?: unknown }[] | null, agencyId: string | null): StudioAccess {
  if (!agencyId || memberships === null) return "unknown";
  return memberships.some((m) => m.tenant_id === agencyId) ? "yes" : "no";
}

async function read(cisApiUrl: string, accessToken: string, fetchImpl: typeof fetch): Promise<{ tenant_id?: unknown }[] | null> {
  try {
    const res = await fetchImpl(`${cisApiUrl}/auth/tenant-memberships`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    // A person with no studio gets 200 {memberships: []}; any refusal (401 / 403 / 429 / 503) is not an answer.
    if (!res.ok) return null;
    const data = (await res.json()) as { memberships?: unknown };
    return Array.isArray(data.memberships) ? (data.memberships as { tenant_id?: unknown }[]) : null;
  } catch {
    return null;
  }
}

/** Whether the "Open Content studio" link should show for this session's agency (see the header). */
export async function studioAccess(
  cisApiUrl: string,
  session: Pick<Session, "at" | "iss" | "sub" | "tid">,
  fetchImpl: typeof fetch = fetch,
  now: number = Date.now(),
): Promise<StudioAccess> {
  if (!cisApiUrl || !session.tid) return "unknown";
  const key = memoKey(session);
  const hit = memo.get(key);
  if (hit && hit.until > now) return hit.value;
  const value = decide(await read(cisApiUrl, session.at, fetchImpl), session.tid);
  if (memo.size >= MEMO_MAX) memo.clear();
  memo.set(key, { until: now + (value === "unknown" ? UNKNOWN_TTL_MS : ANSWER_TTL_MS), value });
  return value;
}

/** The link to render: none when CIS said the agency has no studio for this person. */
export function studioLink(href: string | null, access: StudioAccess): string | null {
  return access === "no" ? null : href;
}
