// portal/app/portal/_components/marketplace/marketplaceApi.ts
// AA-671 — the Marketplace's data layer (react-query wires these in). Tenant calls go through the BFF proxy
// /api/tenant/* (the session cookie names the tenant; no tenant id is ever sent). Shapes follow CIS
// GET /v1/tours/pool, POST /v1/tours/pool/{id}/rewrite (api/routers/v1_tours.py) and GET /v1/marketplace
// (api/routers/v1_marketplace.py).

import { apiGet, apiSend } from "@/app/_kit";
import type { ServedFields } from "../ServedBadge";

/** One Adventure Asia tour of the catalog (GET /v1/tours/pool row). */
export interface PoolTour extends ServedFields {
  id: string;
  tour_id: string;
  aa_name: string;
  aa_subtitle: string | null;
  aa_summary: string | null;
  aa_highlights: string | null;
  aa_itineraries: string | null;
  seo_title: string | null;
  seo_meta: string | null;
  seo_keywords_used: string | null;
  published_at: string | null;
  country: string | null;
  duration: string | null;
  price_raw: string | null;
  already_rewritten: boolean;
}

export interface PoolResponse {
  data?: PoolTour[];
  pagination?: { page?: number; page_size?: number; total?: number; pages?: number };
  countries?: string[];
}

/** One tour the tenant picked (GET /v1/marketplace row: its latest version). */
export interface PickedTour {
  version_id: string;
  version_number: number;
  /** Coarse state (CIS tenant_version_state): writing | failed | approved | rejected | ready. */
  status: string;
  published_tour_id: string;
  tour_id: string;
  name: string;
  country: string | null;
  duration: string | null;
  price_usd: number | null;
  price_available: boolean;
}

export interface PickedResponse {
  posts_per_week: number | null;
  tours: PickedTour[];
  total_tours: number;
}

export const POOL_KEY = "tour-pool";
export const PICKED_KEY = ["tenant-marketplace"] as const;

export function fetchPool(query: string): Promise<PoolResponse> {
  return apiGet<PoolResponse>(`/api/tenant/v1/tours/pool?${query}`);
}

export function fetchPicked(): Promise<PickedResponse> {
  return apiGet<PickedResponse>("/api/tenant/v1/marketplace");
}

// AA-566 Phần D — the per-tour rewrite settings have no tenant UI; the rewrite uses these defaults (unchanged).
const REWRITE_BODY = { rewrite_language: "en-US", seo_mode: "standard", use_brand_rules: true };

export interface RewriteOutcome {
  started: string[];
  failed: { id: string; message: string }[];
}

/** Start a rewrite of each tour, one after the other (the API charges the quota per call); a refused one (409 already
 *  in progress, 409 tour no longer served, 404) is reported, not hidden. */
export async function startRewrites(ids: string[]): Promise<RewriteOutcome> {
  const out: RewriteOutcome = { started: [], failed: [] };
  for (const id of ids) {
    try {
      await apiSend(`/api/tenant/v1/tours/pool/${encodeURIComponent(id)}/rewrite`, { body: REWRITE_BODY });
      out.started.push(id);
    } catch (e) {
      out.failed.push({ id, message: e instanceof Error ? e.message : "Request failed" });
    }
  }
  return out;
}
