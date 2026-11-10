// web/app/(studio)/portal/_components/retiredPagesApi.ts — AA-1039 follow-up (tech lead decision S276)
//
// When Adventure Asia stops offering a tour, nothing changes on the agency's site by default: CIS
// (api/routers/v1_publish.py) lists the agency's live posts about it (GET /v1/publish-log/retired-pages) and moves one
// WordPress page to draft when the agency clicks (POST /v1/publish-log/{publish_id}/move-to-draft; own page with an
// open retirement notice only, 404 otherwise; a repeat click answers "already a draft" with `replayed`). The agency
// can opt in to automatic drafts after N days (GET / PATCH /v1/account/tour-retirement, Content studio owner only).
// Shared by the Publish page section, the bell and the Settings card.
import { apiGet, apiSend, type Translator } from "@/app/_kit";
import type { RETIRED_PAGES_MESSAGES } from "./retiredPages.messages";

export interface RetiredPage {
  publish_id: string;
  piece_id: string;
  title: string;
  channel: string;
  external_url: string | null;
  tour_name: string | null;
  can_move_to_draft: boolean;
  /** The automatic draft date of an agency that turned automatic drafts on; null otherwise. */
  draft_due_at: string | null;
  flagged_at: string | null;
}

export type MoveToDraftOutcome = "drafted" | "already_drafted";

export interface MoveToDraftResult {
  publish_id: string;
  outcome: MoveToDraftOutcome;
  status: "unpublished";
  external_url: string | null;
  replayed: boolean;
  message: string;
}

export interface TourRetirementSettings {
  auto_draft: boolean;
  notice_days: number;
  min_days: number;
  max_days: number;
  default_notice_days: number;
}

/** The Publish page anchor of the section (the bell's notice links to /portal/content/publish#retired-pages). */
export const RETIRED_PAGES_ANCHOR = "retired-pages";

/** Under the publish-log key: a refresh of ["publish-log"] (publish, unpublish, put back) refreshes this list too. */
export const RETIRED_PAGES_QUERY_KEY = ["publish-log", "retired-pages"] as const;

export const TOUR_RETIREMENT_SETTINGS_KEY = ["account", "tour-retirement"] as const;

export function listRetiredPages(): Promise<RetiredPage[]> {
  return apiGet<{ data?: RetiredPage[] }>("/api/tenant/v1/publish-log/retired-pages").then((d) => d.data ?? []);
}

export function moveToDraft(publishId: string): Promise<MoveToDraftResult> {
  return apiSend<MoveToDraftResult>(`/api/tenant/v1/publish-log/${encodeURIComponent(publishId)}/move-to-draft`,
    { method: "POST" });
}

export function getTourRetirementSettings(): Promise<TourRetirementSettings> {
  return apiGet<TourRetirementSettings>("/api/tenant/v1/account/tour-retirement");
}

export function saveTourRetirementSettings(body: { auto_draft: boolean; notice_days?: number }):
  Promise<TourRetirementSettings> {
  return apiSend<TourRetirementSettings>("/api/tenant/v1/account/tour-retirement", { method: "PATCH", body });
}

/** The outcome in the UI language (the API's `message` is English only). */
export function moveOutcomeText(t: Translator<typeof RETIRED_PAGES_MESSAGES.en>, r: MoveToDraftResult): string {
  return r.outcome === "drafted" ? t("drafted") : t("alreadyDrafted");
}

/** The days field: a whole number within the route's bounds, else null (the form shows the bounds). */
export function parseDays(raw: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const n = Number(raw.trim());
  return n >= min && n <= max ? n : null;
}
