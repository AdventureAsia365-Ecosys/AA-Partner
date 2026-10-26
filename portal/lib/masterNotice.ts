// portal/lib/masterNotice.ts
// AA-990 scope 5 — a tenant's tour version is older than Adventure Asia's current text of the tour. CIS
// GET /v1/tours/my-versions and /v1/tours/versions/{id} carry `master_updated_at` and `master_newer` (true when AA
// changed the tour after this version was made). The portal only informs: the tenant's text is never changed, and
// the existing Request Rewrite action is the way to pick up the new details. No React and no imports, so
// `node --test` runs lib/masterNotice.test.ts directly (the AA-761 pattern). Both fields are optional: before the
// CIS change is deployed they are absent and nothing is shown.

/** The fields of a version (catalogApi.Version) these rules read. */
export interface MasterFreshness {
  master_newer?: boolean | null;
  master_updated_at?: string | null;
  served_status?: "served" | "paused" | "retired" | null;
}

/** Short label for the list row (the full sentence is its tooltip and the drawer's notice). */
export const MASTER_NEWER_LABEL = "Updated by Adventure Asia";

/** True when the notice applies: AA updated the tour after this version, and new work on the tour is open (a paused
 *  or retired tour shows its own served badge instead, and a rewrite would be refused there). */
export function showMasterNotice(v: MasterFreshness | null | undefined): boolean {
  if (v?.master_newer !== true) return false;
  return v.served_status !== "retired" && v.served_status !== "paused";
}

/** The notice sentence, or null when there is nothing to say. `fmtDate` formats the ISO time for people (the portal
 *  passes the kit's fmtDate); an empty result drops the "on <date>" part. */
export function masterNoticeText(
  v: MasterFreshness | null | undefined,
  fmtDate: (iso: string) => string,
): string | null {
  if (!showMasterNotice(v)) return null;
  const when = v?.master_updated_at ? fmtDate(v.master_updated_at).trim() : "";
  const on = when && when !== "—" ? ` on ${when}` : "";
  return `Adventure Asia updated this tour${on}, after your version. Your text was not changed. `
    + "Rewrite it if you want the new details.";
}
