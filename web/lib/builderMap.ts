// web/lib/builderMap.ts
// AA-948 (card CRM-20) — what the proposal builder's map draws, from the builder view (pure, no React, no Mapbox).
// Each trip day carries `map_points` (API app/proposals/day_map.py: the places of the version's served whole AA tours
// on that day). The map numbers every point with its day, joins them in day order (the shared map engine's "stops +
// route" layer set, lib/map-engine), and lists the days without a point instead of guessing one (design edge case:
// "a day without a location renders without a pin"). The "Before you go" day is never on the map.
import { lngLat, routeStops, type LngLat, type RouteStop } from "@/lib/map-engine";

export interface MapPoint {
  name: string;
  lat: number;
  lng: number;
  approximate: boolean;
}

/** The part of a builder day the map reads. */
export interface MapDay {
  proposal_day_id: string;
  day_no: number;
  before_you_go: boolean;
  map_points?: MapPoint[] | null;
}

export interface BuilderMapPlan {
  /** One stop per place visit, labelled with its day number, in day order. */
  stops: RouteStop[];
  /** Every point to frame (also the ones a later day shares). */
  frame: LngLat[];
  /** Day number (the stop label) -> that day's id, to select the day from its marker. */
  dayOf: Record<string, string>;
  /** Day number -> the names on that day (with "approximate" marks), for the stop's accessible name. */
  names: Record<string, { name: string; approximate: boolean }[]>;
  /** Trip days without any point, in day order. */
  unplaced: number[];
}

export function builderMapPlan(days: readonly MapDay[]): BuilderMapPlan {
  const trip = days.filter((d) => !d.before_you_go && d.day_no >= 1).sort((a, b) => a.day_no - b.day_no);
  const visits: { label: string; coord: LngLat | null }[] = [];
  const frame: LngLat[] = [];
  const dayOf: Record<string, string> = {};
  const names: BuilderMapPlan["names"] = {};
  const unplaced: number[] = [];
  for (const d of trip) {
    const label = String(d.day_no);
    let placed = false;
    for (const p of d.map_points ?? []) {
      const coord = lngLat(p?.lng, p?.lat);
      if (!coord) continue;
      placed = true;
      visits.push({ label, coord });
      frame.push(coord);
      (names[label] ??= []).push({ name: p.name, approximate: Boolean(p.approximate) });
    }
    if (placed) dayOf[label] = d.proposal_day_id;
    else unplaced.push(d.day_no);
  }
  return { stops: routeStops(visits), frame, dayOf, names, unplaced };
}

/** The theme colours the map paints with (Mapbox paint cannot read CSS variables): the kit tokens, resolved on the
 *  page, so an agency's brand accent is the route colour. A token that is not a plain colour falls back. */
export const MAP_COLOR_VARS = { line: "--aa-accent", stop: "--aa-accent", label: "--aa-on-accent" } as const;
const FALLBACK: Record<keyof typeof MAP_COLOR_VARS, string> = { line: "#DB9628", stop: "#DB9628", label: "#1A1206" };
/** Text of a stop popup: Mapbox draws its popup white in both themes, so the text stays dark. */
export const POPUP_TEXT = "#1F2933";
const COLOUR = /^(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))$/;

export function builderMapColors(read: (cssVar: string) => string | null | undefined): Record<keyof typeof MAP_COLOR_VARS, string> {
  const out = { ...FALLBACK };
  for (const key of Object.keys(MAP_COLOR_VARS) as (keyof typeof MAP_COLOR_VARS)[]) {
    const v = (read(MAP_COLOR_VARS[key]) ?? "").trim();
    if (COLOUR.test(v)) out[key] = v;
  }
  return out;
}
