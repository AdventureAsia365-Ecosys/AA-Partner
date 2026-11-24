// web/lib/aaAddons.ts — AA-1152 (S285 advisory PR-29 T6, B2 option C): the builder's "AA add-on" drawer tab and the
// Settings > Payments "Prices on my planner" panel. The API computes every price (the agency's markup on AA's B2B
// price, else AA's suggested retail price, rounded up per traveller to its step); this file only shapes and words.

export interface AaAddonPlace {
  destination_id: string;
  role: "operates" | "pickup";
  name: string | null;
}

export interface AaAddon {
  component_id: string;
  kind: "transfer" | "guide" | "activity" | "entrance" | "meal";
  title: string;
  unit: "person" | "group" | "vehicle" | "day";
  duration_minutes: number | null;
  min_pax: number;
  operating_weekdays: number[];
  start_times: string[];
  first_date: string;
  places: AaAddonPlace[];
  at_day_place: boolean;
  suggested_per_person: string | null;
  suggested_basis: "agency_markup" | "aa_rrp" | null;
  /** Owner and agent only (the API leaves it null for other roles). */
  b2b_per_person: string | null;
}

export interface AaAddonList {
  date: string | null;
  party: number;
  currency: string;
  place: string | null;
  needs_tour: boolean;
  /** An AA tour is there but its day has no known place yet: nothing can be offered (S291 follow-up). */
  no_tour_place?: boolean;
  /** Only the add-ons sold at a place of the AA tour on this day (the API filters; it refuses any other). */
  items: AaAddon[];
}

/** The notes the drawer shows above the list: why nothing (or nothing more) can be added. */
export function addonNoteKeys(list: AaAddonList): string[] {
  const keys: string[] = [];
  if (list.needs_tour) keys.push("addon.needsTour");
  else if (list.no_tour_place) keys.push("addon.noTourPlace");
  if (!list.date) keys.push("addon.noDate");
  if (list.items.length === 0 && !list.needs_tour && !list.no_tour_place) keys.push("addon.none");
  return keys;
}

export interface PlannerPricesCurrency {
  currency: string;
  step: string;
  chosen: boolean;
  /** The platform default step of the currency (used while the agency chose none). */
  default: string;
  choices: string[];
}

export interface PlannerPricesView {
  show_planner_prices: boolean;
  base_currency: string;
  currencies: PlannerPricesCurrency[];
}

const WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const WEEKDAYS_VI = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"];

/** "Every day", or the ISO weekdays it runs (1 = Monday) in the page's language. */
export function weekdaysText(days: number[], lang: "en" | "vi"): string {
  const sorted = [...new Set(days)].filter((d) => d >= 1 && d <= 7).sort((a, b) => a - b);
  if (sorted.length === 7) return lang === "vi" ? "Hằng ngày" : "Every day";
  const names = lang === "vi" ? WEEKDAYS_VI : WEEKDAYS_EN;
  return sorted.map((d) => names[d - 1]).join(", ");
}

/** "3 h", "1 h 30 min", "45 min" (null = nothing). */
export function durationText(minutes: number | null): string | null {
  if (minutes === null || minutes <= 0) return null;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

/** The places as one line, the operating places first ("Lantern Bay · pick-up: Harbour Hill"). */
export function placesText(places: AaAddonPlace[], pickupLabel: string): string {
  const operates = places.filter((p) => p.role === "operates" && p.name).map((p) => p.name);
  const pickups = places.filter((p) => p.role === "pickup" && p.name).map((p) => p.name);
  const parts = [operates.join(", ")];
  if (pickups.length) parts.push(`${pickupLabel}: ${pickups.join(", ")}`);
  return parts.filter(Boolean).join(" · ");
}

/** The body of a save: only the currencies whose step changed ("" = back to the platform default). */
export function changedSteps(
  currencies: PlannerPricesCurrency[],
  picked: Record<string, string>,
): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const c of currencies) {
    const value = picked[c.currency];
    if (value === undefined) continue;
    if (value === "" && c.chosen) out[c.currency] = null;
    else if (value !== "" && (!c.chosen || value !== c.step)) out[c.currency] = value;
  }
  return out;
}
