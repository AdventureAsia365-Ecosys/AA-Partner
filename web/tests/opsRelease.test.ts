// AA-956 (CRM-26): the "AA ground operations" panel helpers of the trip page: the panel state (nothing confirmed,
// being prepared, live, withdrawn, deleted), the missing items in a fixed order, the activity lines, the BFF lines
// (read and withdraw only: AA ops' routes are never reachable from the agency's browser), the access-log wording and
// both languages.
import { describe, expect, it } from "vitest";
import { ACTIONS, codeLabel } from "@/lib/accessLog";
import { matchBff } from "@/lib/bff";
import { MESSAGES, t, type MessageKey } from "@/lib/i18n";
import { opsReleaseEn, opsReleaseVi } from "@/lib/i18nOpsRelease";
import { activityLines, missingItems, panelState, type OpsRelease, type OpsReleaseView } from "@/lib/opsRelease";

const UUID = "00000000-0000-4000-8000-000000000001";

function release(over: Partial<OpsRelease> = {}): OpsRelease {
  return {
    ops_release_id: UUID,
    status: "live",
    payload_version: 2,
    fields_released: ["name", "age_band"],
    completeness: { travellers: 2, complete: 1, missing: { stage3: 1, waiver: 0, arrival_flight: 1, departure_flight: 0 } },
    complete: false,
    traveller_count: 2,
    released_at: "2026-11-01T02:00:00+00:00",
    refreshed_at: "2026-11-02T02:00:00+00:00",
    purge_at: "2027-03-10",
    days_to_purge: 129,
    t21_alerted_at: null,
    withdrawn_at: null,
    withdraw_reason: null,
    purged_at: null,
    purge_reason: null,
    deletion_receipt: null,
    view_count: 0,
    last_viewed_at: null,
    can_withdraw: true,
    ...over,
  };
}

function view(over: Partial<OpsReleaseView> = {}): OpsReleaseView {
  return { trip_id: UUID, confirmed_aa_lines: 1, release: release(), activity: [], ...over };
}

describe("panel state", () => {
  it("says nothing is shared before an AA tour is confirmed, and pending right after", () => {
    expect(panelState(view({ confirmed_aa_lines: 0, release: null }))).toBe("none");
    expect(panelState(view({ release: null }))).toBe("pending");
  });
  it("follows the release status", () => {
    expect(panelState(view())).toBe("live");
    expect(panelState(view({ release: release({ status: "withdrawn", withdrawn_at: "2026-11-03" }) }))).toBe("withdrawn");
    const purged = release({ status: "purged", withdraw_reason: "trip_cancelled", purged_at: "2026-11-03" });
    expect(panelState(view({ release: purged }))).toBe("purged");
  });
  it("a release withdrawn for cancelled AA lines comes back when a new AA tour is confirmed", () => {
    const old = release({ status: "purged", withdraw_reason: "aa_lines_cancelled", purged_at: "2026-11-03" });
    expect(panelState(view({ release: old }))).toBe("pending");
    expect(panelState(view({ release: old, confirmed_aa_lines: 0 }))).toBe("purged");
    const mine = release({ status: "purged", withdraw_reason: "agency_withdrawn", purged_at: "2026-11-03" });
    expect(panelState(view({ release: mine }))).toBe("purged");
  });
});

describe("completeness and activity", () => {
  it("lists only the missing items, in a fixed order", () => {
    expect(missingItems(release().completeness)).toEqual([
      { item: "stage3", n: 1 },
      { item: "arrival_flight", n: 1 },
    ]);
    expect(missingItems(null)).toEqual([]);
  });
  it("shows the newest activity first, at most the limit", () => {
    const activity = [
      { at: "2026-11-01T02:00:00Z", actor_kind: "system", action: "ops_release_create", reason: "aa_line_confirmed" },
      { at: "2026-11-02T02:00:00Z", actor_kind: "aa_ops", action: "ops_release_view", reason: "ops_release" },
      { at: "2026-11-03T02:00:00Z", actor_kind: "aa_ops", action: "ops_manifest_export", reason: "supplier_manifest" },
    ];
    expect(activityLines(view({ activity }), 2).map((a) => a.action)).toEqual(["ops_manifest_export", "ops_release_view"]);
    expect(activityLines(view({ activity: null }))).toEqual([]);
  });
});

describe("BFF lines", () => {
  it("lets the agency read its panel and withdraw, nothing else", () => {
    expect(matchBff("GET", `trips/${UUID}/ops-release`)?.card).toBe("AA-956");
    expect(matchBff("POST", `trips/${UUID}/ops-release/withdraw`)?.card).toBe("AA-956");
    expect(matchBff("POST", `trips/${UUID}/ops-release`)).toBeNull();
    expect(matchBff("GET", `trips/${UUID}/ops-release/manifest`)).toBeNull();
    expect(matchBff("GET", "ops-releases")).toBeNull();
  });
});

describe("wording", () => {
  it("has every key in both languages", () => {
    expect(Object.keys(opsReleaseVi).sort()).toEqual(Object.keys(opsReleaseEn).sort());
    for (const key of Object.keys(opsReleaseEn)) expect(MESSAGES.vi[key as MessageKey]).toBeTruthy();
  });
  it("names AA ops' views and exports in the access log, filterable", () => {
    expect(ACTIONS).toContain("ops_release_view");
    expect(ACTIONS).toContain("ops_manifest_export");
    expect(codeLabel("en", "log.act", "ops_release_view")).toBe("viewed the AA operations details of");
    expect(codeLabel("vi", "log.actor", "aa_ops")).toBe("Vận hành AA");
    expect(t("en", "task.ops_release_incomplete")).toContain("21 days");
    expect(t("en", "cancelTrip.effect.ops_release_withdrawn")).toContain("{n}");
  });
});
