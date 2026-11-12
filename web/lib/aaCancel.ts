// web/lib/aaCancel.ts
// AA-1070 (crm-13) — Adventure Asia cancelled a departure the trip is on: the shapes of the trip page's guided card
// (GET /trips/{id} `aa_cancel`, api/app/travel_ops/aa_cancel.py) and the pure helpers of its two answers:
//   (a) re-quote the same AA tour on another open departure as a new proposal version (a new trip when the trip is
//       booked or accepted): POST /trips/{id}/aa-cancel/{case}/requote {departure_id, start_date};
//   (b) cancel the trip with the reason "aa_cancelled": the API forces the traveller charge to 0 and prefills full
//       refunds as drafts (the cancel dialog, opened on that reason).
// No personal data here: ids, codes and dates only.
import type { AaDeparture } from "@/lib/aa";

export interface AaCancelTour {
  tour_source_id: string | null;
  component_id: string | null;
  component_kind: string | null;
  title: string;
  service_from: string | null;
}

export interface AaCancelCase {
  case_id: string;
  departure_id: string;
  resolution: "requoted" | "cancelled" | null;
  opened_at: string;
  resolved_at: string | null;
  /** Still to settle: open, or re-quoted onto a new trip while this one is not cancelled yet. */
  active: boolean;
  tour: AaCancelTour | null;
  proposal_id: string | null;
  proposal_version: number | null;
  rebook_trip_id: string | null;
  can_requote: boolean;
  requote_mode: "version" | "new_trip" | null;
  can_cancel: boolean;
}

export interface AaCancelView {
  cases: AaCancelCase[];
  /** The trip this one re-books (a re-quote of a booked trip made it). */
  rebooked_from: string | null;
}

export interface RequoteResult {
  proposal_id: string;
  trip_id: string;
  mode: "version" | "new_trip";
}

/** The departures the agent may pick: open (not cancelled), not the one AA cancelled, from today, soonest first. */
export function requoteChoices(departures: readonly AaDeparture[], cancelled: string, today: string): AaDeparture[] {
  return departures
    .filter((d) => d.status !== "cancelled" && d.departure_id !== cancelled && d.start_date >= today)
    .slice()
    .sort((a, b) => (a.start_date === b.start_date ? a.departure_id.localeCompare(b.departure_id) : a.start_date < b.start_date ? -1 : 1));
}

/** Today (YYYY-MM-DD) in the agency's zone; UTC when the zone is unknown. */
export function agencyToday(timeZone: string, now: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  } catch {
    return now.toISOString().slice(0, 10);
  }
}

/** Where the re-quote's version opens: the builder of the trip that holds it. */
export function requoteHref(r: Pick<RequoteResult, "proposal_id" | "trip_id">): string {
  return `/pipeline/${r.trip_id}/proposal/${r.proposal_id}`;
}

/** The API refusal of a re-quote -> its message key (aaCancel.err.<code>), else the generic one. */
export function requoteErrorKey(detail: unknown): string {
  const code = typeof detail === "string" ? detail : (detail as { code?: unknown } | null)?.code;
  const known = ["same_departure", "departure_cancelled", "no_builder_version", "builder_draft_exists", "aa_cancel_case_closed", "trip_not_open_for_proposal"];
  return typeof code === "string" && known.includes(code) ? `aaCancel.err.${code}` : "aaCancel.err.generic";
}
