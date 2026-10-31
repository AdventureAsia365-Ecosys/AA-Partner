// @vitest-environment jsdom
// S262 live evidence on the trip page (AA-957 / AA-956):
//   - the AA incident banner printed the Booking departure id ("Departure 9f7d807d-… · Cleared …"): it now names the
//     departure by the agency's own booking line on it (title, first service day) or leaves it out;
//   - the "AA ground operations" receipt said "4 views by AA, 4 supplier lists" while the activity listed 3 + 3: the
//     receipt was right, the panel cut the list at 8 lines without saying so. It now offers "Show all n" and, when the
//     API holds older lines than it sent, points to the access log.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { incidentDeparture, type TripIncident } from "@/lib/duty";
import { MESSAGES, t, type MessageKey } from "@/lib/i18n";
import { tripCodesEn, tripCodesVi } from "@/lib/i18nTripCodes";
import { ACTIVITY_SHOWN, activityFooter, type OpsActivity, type OpsRelease, type OpsReleaseView } from "@/lib/opsRelease";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { TripIncidentBanner, OpenIncidents } = await import("@/components/duty/IncidentPanel");
const { OpsReleasePanel } = await import("@/components/pipeline/OpsReleasePanel");

const DEPARTURE = "6a1e0c55-3b7d-4f20-8e44-91c2d0b7a6f3";
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

function incident(over: Partial<TripIncident> = {}): TripIncident {
  return {
    incident_flag_id: "f1",
    departure_id: DEPARTURE,
    departure: { title: "Lotus valley trek 3 days", date: "2026-11-12" },
    incident_code: "road",
    status: "cleared",
    raised_at: "2026-10-30T01:00:00Z",
    cleared_at: "2026-10-31T02:00:00Z",
    ...over,
  };
}

describe("AA incident: the departure in words", () => {
  const tr = (key: MessageKey) => t("en", key);

  it("names the agency's line and its first day, or the title alone, or nothing", () => {
    expect(incidentDeparture({ title: "Lotus valley trek 3 days", date: "2026-11-12" }, "en-AU", tr)).toBe("Lotus valley trek 3 days, from 12 Nov 2026");
    expect(incidentDeparture({ title: "Lotus valley trek 3 days", date: null }, "en-AU", tr)).toBe("Lotus valley trek 3 days");
    expect(incidentDeparture({ title: " ", date: "2026-11-12" }, "en-AU", tr)).toBeNull();
    expect(incidentDeparture(null, "en-AU", tr)).toBeNull();
    expect(incidentDeparture({ title: "Lotus valley trek", date: "2026-11-12" }, "vi-VN", (k) => t("vi", k))).toBe("Lotus valley trek, từ 12 thg 11, 2026");
  });

  it("the trip banner never prints the Booking departure id", () => {
    const html = renderToStaticMarkup(createElement(TripIncidentBanner, { incidents: [incident()], lang: "en", locale: "en-AU" }));
    expect(html).not.toMatch(UUID);
    expect(html).toContain("Lotus valley trek 3 days, from 12 Nov 2026 · Cleared 31 Oct 2026");
    // An older API answer (no departure) or a line without a title: the departure is left out, not shown as an id.
    const bare = renderToStaticMarkup(createElement(TripIncidentBanner, { incidents: [incident({ departure: undefined })], lang: "en", locale: "en-AU" }));
    expect(bare).not.toMatch(UUID);
    expect(bare).toContain("Cleared 31 Oct 2026");
  });

  it("the Travelling now banner names the departure too, and keeps the id only as a test hook", () => {
    const open = { ...incident({ status: "open", cleared_at: null }), trips: [{ trip_id: "t1", title: "Lotus coast", stage: "travelling", start_date: null, end_date: null }] };
    const html = renderToStaticMarkup(createElement(OpenIncidents, { incidents: [open], lang: "vi", locale: "vi-VN" }));
    const text = html.replace(/<[^>]+>/g, " ");
    expect(text).not.toMatch(UUID);
    expect(text).toContain("Lotus valley trek 3 days, từ 12 thg 11, 2026");
  });

  it("has every new key in both languages", () => {
    expect(Object.keys(tripCodesVi).sort()).toEqual(Object.keys(tripCodesEn).sort());
    for (const key of Object.keys(tripCodesEn)) expect(MESSAGES.vi[key as MessageKey]).toBe(tripCodesVi[key as keyof typeof tripCodesVi]);
  });
});

function release(over: Partial<OpsRelease> = {}): OpsRelease {
  return {
    ops_release_id: "r1",
    status: "purged",
    payload_version: 1,
    fields_released: ["name"],
    completeness: { travellers: 1, complete: 1, missing: { stage3: 0, waiver: 0, arrival_flight: 0, departure_flight: 0 } },
    complete: true,
    traveller_count: 1,
    released_at: "2026-10-31T10:00:00+00:00",
    refreshed_at: "2026-10-31T10:00:00+00:00",
    purge_at: null,
    days_to_purge: null,
    t21_alerted_at: null,
    withdrawn_at: "2026-10-31T21:04:03+00:00",
    withdraw_reason: "trip_cancelled",
    purged_at: "2026-10-31T21:04:05+00:00",
    purge_reason: "withdrawn",
    deletion_receipt: { payload_version: 1, travellers: 1, manifest_exports: 4, views: 4, reason: "withdrawn" },
    view_count: 4,
    last_viewed_at: "2026-10-31T20:00:00+00:00",
    can_withdraw: false,
    ...over,
  };
}

/** The live case: shared, 4 views + 4 supplier lists, withdrawn, deleted: 11 lines, newest first. */
function evidenceActivity(): OpsActivity[] {
  const at = (min: number) => `2026-10-31T${String(10 + Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}:00+00:00`;
  const lines: OpsActivity[] = [{ at: at(0), actor_kind: "system", action: "ops_release_create", reason: "aa_line_confirmed" }];
  for (let i = 1; i <= 4; i++) {
    lines.push({ at: at(i * 10), actor_kind: "aa_ops", action: "ops_release_view", reason: "ops_release" });
    lines.push({ at: at(i * 10 + 1), actor_kind: "aa_ops", action: "ops_manifest_export", reason: "supplier_manifest" });
  }
  lines.push({ at: at(100), actor_kind: "member", action: "ops_release_withdraw", reason: "trip_cancelled" });
  lines.push({ at: at(101), actor_kind: "system", action: "ops_release_purge", reason: "withdrawn" });
  return lines.reverse();
}

function view(over: Partial<OpsReleaseView> = {}): OpsReleaseView {
  return { trip_id: "trip-1", confirmed_aa_lines: 0, release: release(), activity: evidenceActivity(), ...over };
}

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(element);
  });
  return container;
}

const count = (c: HTMLElement, text: string) => [...c.querySelectorAll("li")].filter((li) => li.textContent?.includes(text)).length;

describe("AA ground operations: the activity agrees with the receipt", () => {
  it("says when lines are hidden and when the API holds older ones", () => {
    expect(activityFooter(view(), false)).toEqual({ toggle: "all", total: 11, older: false });
    expect(activityFooter(view(), true)).toEqual({ toggle: "fewer", total: 11, older: false });
    expect(activityFooter(view({ activity: evidenceActivity().slice(0, ACTIVITY_SHOWN) }), false).toggle).toBeNull();
    expect(activityFooter(view({ activity: null }), false)).toEqual({ toggle: null, total: 0, older: false });
    expect(activityFooter(view({ activity_more: true }), false).older).toBe(true);
  });

  it("shows the newest 8 with 'Show all 11'; after it, 4 opened and 4 downloaded, as on the receipt", async () => {
    const c = await mount(createElement(OpsReleasePanel, { tripId: "trip-1", view: view(), lang: "en", locale: "en-AU" }));
    expect(c.querySelector('[data-testid="ops-release-receipt"]')!.textContent).toContain("4 views by AA, 4 supplier lists");
    expect(c.querySelectorAll("li")).toHaveLength(ACTIVITY_SHOWN);
    expect(count(c, t("en", "ops.line.ops_release_view"))).toBe(3);
    const toggle = c.querySelector('[data-testid="ops-activity-toggle"] button') as HTMLButtonElement;
    expect(toggle.textContent).toBe("Show all 11");
    await act(async () => toggle.click());
    expect(c.querySelectorAll("li")).toHaveLength(11);
    expect(count(c, t("en", "ops.line.ops_release_view"))).toBe(4);
    expect(count(c, t("en", "ops.line.ops_manifest_export"))).toBe(4);
    expect(toggle.textContent).toBe("Show fewer");
    expect(c.querySelector('[data-testid="ops-activity-older"]')).toBeNull();
  });

  it("points to the access log when older lines exist", async () => {
    const c = await mount(createElement(OpsReleasePanel, { tripId: "trip-1", view: view({ activity_more: true }), lang: "vi", locale: "vi-VN" }));
    const older = c.querySelector('[data-testid="ops-activity-older"]')!;
    expect(older.textContent).toContain(t("vi", "ops.activityOlder"));
    expect(older.querySelector("a")!.getAttribute("href")).toBe("/settings/access-log");
  });
});
