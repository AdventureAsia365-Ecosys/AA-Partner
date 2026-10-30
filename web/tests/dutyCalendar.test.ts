// AA-961 (CRM-30): the duty of care and calendar helpers of the web: region labels, the bulk reveal reason threshold
// and error wording, one traveller row with every trip (card E), the calendar grouping and month window, the feed
// banner state (card E: a revoked link says so), the BFF lines, the nav entries and both languages.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { byDay, feedState, filterKinds, monthWindow, shiftMonth, type CalendarEvent, type Feed } from "@/lib/calendar";
import {
  availableFields,
  needsReason,
  regionLabel,
  revealErrorKey,
  tripsOf,
  type DutyTraveller,
  type DutyTrip,
} from "@/lib/duty";
import { MESSAGES, t, type MessageKey } from "@/lib/i18n";
import { sectionByKey, visibleSections } from "@/lib/nav";

const UUID = "00000000-0000-4000-8000-000000000001";
const tr = (key: MessageKey) => t("en", key);

function trip(id: string): DutyTrip {
  return {
    trip_id: id,
    title: `Trip ${id}`,
    stage: "travelling",
    start_date: "2026-11-10",
    end_date: "2026-11-20",
    owner_member_id: null,
    is_mine: false,
    on_date: "2026-11-12",
    day_no: 3,
    place: null,
    lines: [],
    traveler_ids: ["p1"],
    incident_flag_id: null,
  };
}

const traveller: DutyTraveller = {
  traveler_id: "p1",
  trip_ids: ["b", "a"],
  regions: ["VN"],
  has_phone: true,
  has_email: false,
  has_emergency_contact: true,
  has_health_info: false,
};

describe("duty of care", () => {
  it("names regions and falls back for an unknown code", () => {
    expect(regionLabel("VN", tr)).toBe("Vietnam");
    expect(regionLabel("unknown", tr)).toBe("Region not known");
    expect(regionLabel("JP", tr)).toBe("JP");
  });

  it("asks for a reason above five travellers (card R) and words each refusal", () => {
    expect(needsReason(5)).toBe(false);
    expect(needsReason(6)).toBe(true);
    expect(revealErrorKey(429, "reveal_rate_limited")).toBe("duty.reveal.rateLimited");
    expect(revealErrorKey(422, "reason_required")).toBe("duty.reveal.reasonRequired");
    expect(revealErrorKey(422, "too_many_travelers")).toBe("duty.reveal.tooMany");
    expect(revealErrorKey(500, "error")).toBe("duty.reveal.failed");
  });

  it("lists a traveller once with every trip (card E) and offers only the fields on file", () => {
    const view = { regions: [{ region: "VN", trips: [trip("a"), trip("b")], traveller_count: 1 }] };
    expect(tripsOf(view, traveller).map((x) => x.trip_id)).toEqual(["b", "a"]);
    expect(availableFields(traveller)).toEqual(["display_name", "phone", "emergency_contact"]);
  });
});

const ev = (date: string, kind: CalendarEvent["kind"], id: string): CalendarEvent => ({
  kind,
  id,
  date,
  all_day: true,
  trip_id: null,
  title: null,
});

describe("calendar", () => {
  it("groups by day in order and filters kinds", () => {
    const events = [ev("2026-11-01", "departure", "1"), ev("2026-11-01", "task", "2"), ev("2026-11-03", "call", "3")];
    expect(byDay(events).map((d) => [d.date, d.events.length])).toEqual([
      ["2026-11-01", 2],
      ["2026-11-03", 1],
    ]);
    expect(filterKinds(events, new Set(["call"])).map((e) => e.id)).toEqual(["3"]);
    expect(filterKinds(events, new Set()).length).toBe(3);
  });

  it("builds month windows", () => {
    expect(monthWindow("2026-02")).toEqual({ from: "2026-02-01", to: "2026-02-28" });
    expect(monthWindow("2028-02")?.to).toBe("2028-02-29");
    expect(monthWindow("2026-13")).toBeNull();
    expect(shiftMonth("2026-12", 1)).toBe("2027-01");
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
  });

  it("says when a feed was revoked, and by whom (card E)", () => {
    const base: Feed = {
      calendar_feed_id: UUID,
      member_id: UUID,
      scope: "mine",
      status: "active",
      created_at: "2026-10-30T00:00:00Z",
      expires_at: "2027-10-30T00:00:00Z",
      revoked_at: null,
      revoked_by_owner: false,
      last_fetched_at: null,
      fetch_count: 0,
    };
    expect(feedState(null)).toBe("none");
    expect(feedState(base)).toBe("active");
    expect(feedState({ ...base, status: "revoked", revoked_at: "2026-10-31T00:00:00Z" })).toBe("revoked");
    expect(feedState({ ...base, status: "revoked", revoked_by_owner: true })).toBe("revoked_by_owner");
  });
});

describe("routes, nav and copy", () => {
  it("allows exactly the duty and calendar calls", () => {
    expect(matchBff("GET", "duty/travelling")?.card).toBe("AA-961");
    expect(matchBff("GET", "duty/reach")?.tenant).toBe(true);
    expect(matchBff("POST", "duty/reveal")?.card).toBe("AA-961");
    expect(matchBff("GET", `trips/${UUID}/incidents`)?.card).toBe("AA-961");
    expect(matchBff("GET", "calendar")?.card).toBe("AA-961");
    expect(matchBff("GET", "calendar/feed")?.card).toBe("AA-961");
    expect(matchBff("GET", "calendar/feeds")?.card).toBe("AA-961");
    expect(matchBff("POST", "calendar/feed")?.card).toBe("AA-961");
    expect(matchBff("POST", `calendar/feeds/${UUID}/revoke`)?.card).toBe("AA-961");
    expect(matchBff("DELETE", "calendar/feed")).toBeNull();
    expect(matchBff("GET", "duty/reveal")).toBeNull();
    expect(matchBff("POST", "calendar/feeds/not-a-uuid/revoke")).toBeNull();
  });

  it("shows Travelling now and Calendar to every role", () => {
    for (const role of ["owner", "agent", "assistant"] as const) {
      const keys = visibleSections(role).map((s) => s.key);
      expect(keys).toContain("travelling");
      expect(keys).toContain("calendar");
    }
    expect(sectionByKey("travelling")?.empty).toBeUndefined(); // a real page, not the empty state
  });

  it("has every new string in both languages, the feed copy never promising names", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("duty.") || k.startsWith("calendar."));
    expect(keys.length).toBeGreaterThan(100);
    for (const k of keys) expect(MESSAGES.vi[k as MessageKey]).toBeTruthy();
    expect(MESSAGES.en["calendar.feed.body"]).toContain("never client names");
  });
});
