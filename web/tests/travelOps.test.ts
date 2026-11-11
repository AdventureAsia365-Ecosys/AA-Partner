// AA-1070: the travel ops helpers (rooming body, tones, overdue, error keys, paths) and every code the pages word
// dynamically has an EN and a VI label (room types, bed preferences, visa types and statuses, passport statuses,
// departure states, manifest fields, the portal's client.* copies). Invented names only.
import { describe, expect, it } from "vitest";
import { MESSAGES, type Lang, type MessageKey } from "@/lib/i18n";
import { clientMessages } from "@/lib/clientMessages";
import { boardHref, parseFilter } from "@/lib/pipeline";
import { BFF_ROUTES } from "@/lib/bff";
import {
  BED_PREFS,
  DEPARTURE_STATES,
  MANIFEST_FIELDS,
  PASSPORT_STATUSES,
  ROOM_TYPES,
  VISA_MOVES,
  VISA_STATUSES,
  VISA_TYPES,
  departurePath,
  departureTone,
  passportTone,
  paxTotal,
  roomingBody,
  travelErrorKey,
  manifestNeedsReason,
  visaOverdue,
  visaTone,
} from "@/lib/travelOps";

const A = "00000000-0000-4000-8000-0000000000a1";
const B = "00000000-0000-4000-8000-0000000000b1";
const LANGS: Lang[] = ["en", "vi"];

describe("rooming body", () => {
  it("keeps placed travellers and drops empty rooms", () => {
    const out = roomingBody([
      { room_no: 1, room_type: "twin", travellers: [{ traveler_id: A, bed_pref: "twin_beds" }, { traveler_id: B }] },
      { room_no: 2, room_type: "single", travellers: [] },
    ]);
    expect(out).toEqual({ ok: true, body: { rooms: [{ room_no: 1, room_type: "twin", travellers: [{ traveler_id: A, bed_pref: "twin_beds" }, { traveler_id: B, bed_pref: null }] }] } });
  });
  it("refuses an overfull room, a traveller twice and a number twice", () => {
    expect(roomingBody([{ room_no: 1, room_type: "single", travellers: [{ traveler_id: A }, { traveler_id: B }] }])).toEqual({ ok: false, error: "room_over_capacity" });
    expect(roomingBody([{ room_no: 1, room_type: "single", travellers: [{ traveler_id: A }] }, { room_no: 2, room_type: "single", travellers: [{ traveler_id: A }] }])).toEqual({ ok: false, error: "traveler_twice" });
    expect(roomingBody([{ room_no: 1, room_type: "single", travellers: [{ traveler_id: A }] }, { room_no: 1, room_type: "single", travellers: [{ traveler_id: B }] }])).toEqual({ ok: false, error: "room_no_twice" });
  });
});

describe("helpers", () => {
  it("tones, totals, paths and errors", () => {
    expect(passportTone("ok")).toBe("success");
    expect(passportTone("expires_too_soon")).toBe("danger");
    expect(passportTone("missing")).toBe("warning");
    expect(passportTone(null)).toBe("neutral");
    expect(departureTone("guaranteed")).toBe("success");
    expect(departureTone("cancelled")).toBe("danger");
    expect(departureTone("scheduled")).toBe("neutral");
    expect(VISA_STATUSES.map(visaTone)).toEqual(["warning", "warning", "info", "success", "danger", "success", "neutral"]);
    expect(paxTotal({ adults: 2, children: 1, infants: 1 })).toBe(4);
    expect(departurePath("DEP 1/2")).toBe("/departures/DEP%201%2F2");
    expect(travelErrorKey("legal_hold")).toBe("travel.err.legal_hold");
    expect(travelErrorKey("whatever")).toBe("travel.err.generic");
    expect(travelErrorKey("reason_required")).toBe("travel.err.reason_required");
    expect(travelErrorKey("reveal_rate_limited")).toBe("travel.err.reveal_rate_limited");
    expect(manifestNeedsReason(5, ["name", "room"])).toBe(false);
    expect(manifestNeedsReason(6, ["name"])).toBe(true);
    expect(manifestNeedsReason(2, ["name", "dietary"])).toBe(true);
    expect(manifestNeedsReason(2, ["mobility"])).toBe(true);
    expect(travelErrorKey(undefined)).toBe("travel.err.generic");
  });
  it("an open visa past its deadline is overdue", () => {
    expect(visaOverdue({ status: "to_do", deadline: "2031-01-01" }, "2031-01-02")).toBe(true);
    expect(visaOverdue({ status: "submitted", deadline: "2031-01-01" }, "2031-01-02")).toBe(false);
    expect(visaOverdue({ status: "collecting", deadline: null }, "2031-01-02")).toBe(false);
  });
  it("the visa moves never lead back from cancelled", () => {
    expect(VISA_MOVES.cancelled).toEqual([]);
    for (const targets of Object.values(VISA_MOVES)) for (const to of targets) expect(VISA_STATUSES).toContain(to);
  });
  it("the pipeline passport filter round-trips", () => {
    expect(parseFilter({ issue: "passport" }).issue).toBe("passport");
    expect(parseFilter({ issue: "x" }).issue).toBeNull();
    expect(boardHref({ owner: "me", issue: "passport" })).toBe("/pipeline?owner=me&issue=passport");
  });
  it("the browser may call the travel routes", () => {
    const allowed = (method: string, path: string) => BFF_ROUTES.some((r) => r.method === method && r.pattern.test(path));
    expect(allowed("GET", "departures")).toBe(true);
    expect(allowed("GET", "departures/DEP-1/roster")).toBe(true);
    expect(allowed("POST", "departures/DEP-1/manifest")).toBe(true);
    for (const sub of ["passports", "rooming", "vouchers", "visas", "visas/suggestions"]) expect(allowed("GET", `trips/${A}/${sub}`)).toBe(true);
    expect(allowed("PUT", `trips/${A}/rooming`)).toBe(true);
    expect(allowed("POST", `trips/${A}/pdfs`)).toBe(true);
    expect(allowed("PUT", `lines/${A}/voucher`)).toBe(true);
    expect(allowed("PATCH", `visa-checklist/${A}`)).toBe(true);
    expect(allowed("PUT", "settings/travel")).toBe(true);
    expect(allowed("DELETE", "settings/travel")).toBe(false);
  });
});

describe("every travel code has a label in both languages", () => {
  const keys = [
    ...ROOM_TYPES.map((k) => `travel.room.${k}`),
    ...ROOM_TYPES.map((k) => `client.travel.roomType.${k}`),
    ...BED_PREFS.map((k) => `travel.bed.${k}`),
    ...VISA_TYPES.map((k) => `travel.visaType.${k}`),
    ...VISA_STATUSES.map((k) => `travel.visaStatus.${k}`),
    ...VISA_STATUSES.map((k) => `client.travel.visaStatus.${k}`),
    ...PASSPORT_STATUSES.map((k) => `travel.passport.status.${k}`),
    ...DEPARTURE_STATES.map((k) => `travel.departure.state.${k}`),
    ...MANIFEST_FIELDS.map((k) => `travel.manifest.field.${k}`),
    ...["room_over_capacity", "traveler_twice", "room_no_twice"].map((k) => `client.travel.err.${k}`),
    "travel.voucher.reason.trip_not_booked",
    "travel.voucher.reason.line_hidden",
    "task.passport_invalid_for_trip",
    "task.visa_deadline_overdue",
    "wf.tpl.passport_invalid_for_trip",
    "wf.event.passport.invalid_for_trip",
    "nav.departures",
    "travel.err.reason_required",
    "travel.err.reveal_rate_limited",
    "travel.manifest.reasonNote",
    "client.travel.trip",
    "client.travel.leadTraveller",
    "client.travel.party",
  ];
  it.each(LANGS)("%s", (lang) => {
    const missing = keys.filter((k) => !MESSAGES[lang][k as MessageKey]?.trim());
    expect(missing).toEqual([]);
  });
  it("the portal gets its travel words", () => {
    expect(clientMessages("vi")["client.travel.vouchers" as MessageKey]).toBe("Phiếu dịch vụ");
    expect(clientMessages("en")["travel.title" as MessageKey]).toBeUndefined();
  });
});
