// web/lib/opsRelease.ts
// AA-956 (card CRM-26, Module 8) — the types GET /crm/v1/trips/{id}/ops-release returns and the pure helpers of the
// trip page's "AA ground operations" panel: whether AA's ground team holds the trip's details, how complete they are,
// when they are deleted (return + 90 days) or the deletion receipt. Counts, codes and dates only: the document itself
// never reaches the agency's browser (the agency has the source data). Edge-safe, unit-tested.

export const MISSING_ITEMS = ["stage3", "waiver", "arrival_flight", "departure_flight"] as const;
export type MissingItem = (typeof MISSING_ITEMS)[number];
export type ReleaseStatus = "live" | "withdrawn" | "purged";

export interface Completeness {
  travellers: number;
  complete: number;
  missing: Partial<Record<MissingItem, number>>;
}

export interface OpsRelease {
  ops_release_id: string;
  status: ReleaseStatus;
  payload_version: number;
  fields_released: string[];
  completeness: Completeness;
  complete: boolean;
  traveller_count: number;
  released_at: string;
  refreshed_at: string;
  purge_at: string | null;
  days_to_purge: number | null;
  t21_alerted_at: string | null;
  withdrawn_at: string | null;
  withdraw_reason: "trip_cancelled" | "aa_lines_cancelled" | "agency_withdrawn" | null;
  purged_at: string | null;
  purge_reason: "retention" | "withdrawn" | null;
  deletion_receipt: {
    payload_version: number;
    travellers: number;
    manifest_exports: number;
    views: number;
    reason: string;
  } | null;
  view_count: number;
  last_viewed_at: string | null;
  can_withdraw: boolean;
}

export interface OpsActivity {
  at: string;
  actor_kind: string;
  action: string;
  reason: string | null;
}

export interface OpsReleaseView {
  trip_id: string;
  confirmed_aa_lines: number;
  release: OpsRelease | null;
  activity: OpsActivity[] | null;
}

/** What the panel says first: nothing confirmed, being prepared, or the release's own status. */
export function panelState(view: OpsReleaseView): "none" | "pending" | ReleaseStatus {
  if (view.release) {
    // A receipt of a release withdrawn because its AA lines were cancelled, while a new AA line is confirmed: the
    // next run releases again.
    const r = view.release;
    if (r.status === "purged" && r.withdraw_reason === "aa_lines_cancelled" && view.confirmed_aa_lines > 0) return "pending";
    return r.status;
  }
  return view.confirmed_aa_lines > 0 ? "pending" : "none";
}

/** The missing items with a count above zero, in a fixed order. */
export function missingItems(c: Completeness | null | undefined): { item: MissingItem; n: number }[] {
  if (!c) return [];
  return MISSING_ITEMS.map((item) => ({ item, n: c.missing?.[item] ?? 0 })).filter((x) => x.n > 0);
}

/** The panel's activity lines: the API's ops audit lines, newest first, at most ``limit``. */
export function activityLines(view: OpsReleaseView, limit = 8): OpsActivity[] {
  return [...(view.activity ?? [])].sort((a, b) => b.at.localeCompare(a.at)).slice(0, limit);
}
