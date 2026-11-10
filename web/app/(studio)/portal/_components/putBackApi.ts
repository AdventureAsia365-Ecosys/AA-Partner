// web/app/(studio)/portal/_components/putBackApi.ts — AA-1039 (option A)
//
// "Put back online": after Adventure Asia serves a tour again, a WordPress page it moved to draft while the tour was
// off sale stays in draft until the tenant clicks. CIS (api/routers/v1_publish.py) lists the pages
// (GET /v1/publish-log/put-back) and puts one back with one WordPress publish call (POST .../{publish_id}/put-back):
// it reads the post first, publishes a draft, only records a page already live, and leaves anything else as is. A
// repeat click answers from the earlier one (`replayed`). Shared by the Publish page section and the bell.
import { apiGet, apiSend, type Translator } from "@/app/_kit";
import type { PUT_BACK_MESSAGES } from "./putBack.messages";

export interface PutBackPage {
  publish_id: string;
  piece_id: string;
  title: string;
  channel: string;
  external_url: string | null;
  tour_name: string | null;
  drafted_at: string | null;
}

export type PutBackOutcome = "published" | "already_online" | "changed_on_wordpress";

export interface PutBackResult {
  publish_id: string;
  outcome: PutBackOutcome;
  wordpress_status: string | null;
  status: "published" | "unpublished";
  external_url: string | null;
  replayed: boolean;
  message: string;
}

/** The Publish page anchor of the section (the bell's restore notice links to /portal/content/publish#put-back). */
export const PUT_BACK_ANCHOR = "put-back";

/** Under the publish-log key: an Unpublish / publish refresh of ["publish-log"] refreshes this list too. */
export const PUT_BACK_QUERY_KEY = ["publish-log", "put-back"] as const;

export function listPutBack(): Promise<PutBackPage[]> {
  return apiGet<{ data?: PutBackPage[] }>("/api/tenant/v1/publish-log/put-back").then((d) => d.data ?? []);
}

export function putBack(publishId: string): Promise<PutBackResult> {
  return apiSend<PutBackResult>(`/api/tenant/v1/publish-log/${encodeURIComponent(publishId)}/put-back`,
    { method: "POST" });
}

const STATE_KEYS = ["private", "pending", "future", "trash", "missing"] as const;

/** The outcome in the UI language (the API's `message` is English only). */
export function outcomeText(t: Translator<typeof PUT_BACK_MESSAGES.en>, r: PutBackResult): string {
  if (r.outcome === "published") return t("published");
  if (r.outcome === "already_online") return t("alreadyOnline");
  const s = (STATE_KEYS as readonly string[]).includes(r.wordpress_status ?? "")
    ? (`state${r.wordpress_status}` as `state${(typeof STATE_KEYS)[number]}`)
    : "stateother";
  return t("leftAsIs", t(s));
}

/** A page left online or put back: success tone; left as is: a neutral note. */
export function isOnline(r: PutBackResult): boolean {
  return r.outcome !== "changed_on_wordpress";
}
