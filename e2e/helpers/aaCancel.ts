// e2e/helpers/aaCancel.ts — AA-1087 / AA-1070 crm-13 (S278 lane l18): the pure parts of tests/aa-cancel.spec.ts (the
// AA-Booking seeder's output, the board's AA cancelled filter), unit-tested in unit/aa-cancel-helpers.spec.ts.

/** What this spec reads from the AA-Booking seeder's APPLY output. */
export interface SeededDeparture {
  departureId: string;
  startDate: string;
  tourSourceId: string;
  componentId: string;
  crmAgency: boolean;
}

/** The seeder's APPLY output -> the departure this run is about. Throws with what is missing. */
export function readSeeded(raw: string): SeededDeparture {
  const parsed = JSON.parse(raw) as {
    fixtures?: {
      departure_id?: string;
      departure_start?: string;
      tour_source_id?: string;
      tour_component_id?: string;
      agency?: { crm_e2e?: boolean };
    };
  };
  const f = parsed.fixtures;
  if (!f) throw new Error('not the seeder\'s APPLY output (no "fixtures")');
  const seeded = {
    departureId: f.departure_id ?? "",
    startDate: f.departure_start ?? "",
    tourSourceId: f.tour_source_id ?? "",
    componentId: f.tour_component_id ?? "",
    crmAgency: f.agency?.crm_e2e === true,
  };
  if (!/^[0-9a-f-]{36}$/.test(seeded.departureId)) throw new Error("fixtures.departure_id is not a UUID");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(seeded.startDate)) throw new Error("fixtures.departure_start is not a date");
  if (!seeded.tourSourceId || !seeded.componentId) throw new Error("fixtures.tour_source_id / tour_component_id missing");
  return seeded;
}

/** The trip ids the board lists under issue=aa_cancelled (open stages and the Closed group). */
export function flaggedTripIds(board: { stages?: { cards?: { trip_id?: string }[] }[]; closed?: { cards?: { trip_id?: string }[] } }): string[] {
  const open = (board.stages ?? []).flatMap((s) => s.cards ?? []);
  return [...open, ...(board.closed?.cards ?? [])].map((c) => c.trip_id ?? "").filter(Boolean);
}
