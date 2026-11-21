// web/lib/agencyItems.ts
// AA-1144 — what an agency may sell of its own (root ADR 0019 b6 as amended): a flight, a visa, travel insurance or
// its own service fee, typed by hand, never sent to AA. Hotels and extra nights, transfers and vehicles, guides,
// tickets, meals and activities are Adventure Asia's: a lodging / activity / transport / meal / guide block of the
// builder takes AA items only (the API answers 422 agency_item_not_allowed / invalid_category). The same lists as the
// API (api/app/ledger/agency_items.py) and the database (crm-080). Pure: no React.

import type { BlockType } from "@/lib/builder";

export const AGENCY_ITEM_CATEGORIES = ["flight", "visa", "insurance", "fee"] as const;
export type AgencyItemCategory = (typeof AGENCY_ITEM_CATEGORIES)[number];

/** AA-1147 (S285 decision T18): only a flight or the agency fee may be folded into the tour price; visa and travel
 * insurance always show their own price (the API answers 422 bundle_not_allowed; crm-083 option_bundled_chk). */
export const BUNDLE_CATEGORIES: readonly AgencyItemCategory[] = ["flight", "fee"];

export function canBundle(category: string | null | undefined): boolean {
  return BUNDLE_CATEGORIES.includes(category as AgencyItemCategory);
}

/** What the builder sends: the box ticked and the type allows it. */
export function bundledValue(checked: boolean, category: string | null | undefined): boolean {
  return checked && canBundle(category);
}

/** The builder blocks an agency item may sit on. */
export const AGENCY_BLOCK_TYPES: readonly BlockType[] = ["other"];

export type DrawerTab = "aa" | "agency" | "catalogue" | "ai";

/** The builder drawer's sources for a block: the agency's own items only on an Other block. */
export function drawerTabs(blockType: BlockType): readonly DrawerTab[] {
  return AGENCY_BLOCK_TYPES.includes(blockType) ? ["aa", "agency", "catalogue", "ai"] : ["aa", "ai"];
}

/** A lodging block is where a client's wish for nights before or after the tour turns into a request to AA (B11). */
export function asksAaForNights(blockType: BlockType): boolean {
  return blockType === "lodging";
}

export const EXTRA_NIGHT_POSITIONS = ["before", "after"] as const;
export type ExtraNightPosition = (typeof EXTRA_NIGHT_POSITIONS)[number];

/** The bounds of the crm-080 CHECKs (the API words the same refusals). */
export const EXTRA_NIGHT_BOUNDS = { nights: [1, 60], rooms: [1, 30], adults: [1, 60], children: [0, 60] } as const;
export const EXTRA_NIGHT_PLACE_MAX = 80;

export interface ExtraNightRequest {
  request_id: string;
  trip_id: string;
  position: ExtraNightPosition;
  place: string;
  check_in: string;
  check_out: string;
  nights: number;
  rooms: number;
  adults: number;
  children: number;
  status: "open" | "closed";
  close_reason: "quoted" | "not_needed" | null;
  closed_at: string | null;
  created_at: string | null;
}

export interface ExtraNightForm {
  position: string;
  place: string;
  check_in: string;
  nights: string;
  rooms: string;
  adults: string;
  children: string;
}

function whole(value: string, [low, high]: readonly [number, number]): number | null {
  if (!/^\d{1,3}$/.test(value.trim())) return null;
  const n = Number(value.trim());
  return n >= low && n <= high ? n : null;
}

/** The request body, or the first field that is not right (the form shows one plain sentence for it). */
export function extraNightBody(form: ExtraNightForm): { body: Record<string, unknown> } | { invalid: keyof ExtraNightForm } {
  const place = form.place.replace(/\s+/g, " ").trim();
  if (!(EXTRA_NIGHT_POSITIONS as readonly string[]).includes(form.position)) return { invalid: "position" };
  if (!place || place.length > EXTRA_NIGHT_PLACE_MAX) return { invalid: "place" };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.check_in)) return { invalid: "check_in" };
  const counts: Record<string, number> = {};
  for (const key of ["nights", "rooms", "adults", "children"] as const) {
    const n = whole(form[key] || (key === "children" ? "0" : ""), EXTRA_NIGHT_BOUNDS[key]);
    if (n === null) return { invalid: key };
    counts[key] = n;
  }
  return { body: { position: form.position, place, check_in: form.check_in, ...counts } };
}
