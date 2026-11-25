// AA-948 (card CRM-20): what the proposal builder's map draws (lib/builderMap.ts), the token it may use
// (lib/mapConfig.ts), and the guards of the shared map engine copy (lib/map-engine: integrity against its MANIFEST.json,
// and the import guard: no TripPlanner app import, mapbox-gl only through the engine). Invented places only.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { builderMapColors, builderMapPlan, type MapDay } from "@/lib/builderMap";
import { MESSAGES } from "@/lib/i18n";
import { MAPBOX_TOKEN_ENV, builderMapToken } from "@/lib/mapConfig";

const WEB = path.resolve(__dirname, "..");

function day(n: number, points: MapDay["map_points"] = []): MapDay {
  return { proposal_day_id: `day-${n}`, day_no: n, before_you_go: n === 0, map_points: points };
}
const JUNIPER = { name: "Juniper Pass", lat: 27.47, lng: 89.64, approximate: false };
const MOSS = { name: "Mosswater", lat: 27.52, lng: 89.71, approximate: true };

describe("builderMapPlan", () => {
  it("numbers each placed day, joins them in day order and lists the days without a place", () => {
    const plan = builderMapPlan([day(3, [MOSS]), day(0, [JUNIPER]), day(1, [JUNIPER]), day(2), day(4, [MOSS])]);
    expect(plan.stops).toEqual([
      { label: "1", coord: [89.64, 27.47] },
      { label: "3", coord: [89.71, 27.52] }, // day 4 is at the same place: no second stop
    ]);
    expect(plan.frame).toHaveLength(3);
    expect(plan.dayOf).toEqual({ "1": "day-1", "3": "day-3", "4": "day-4" });
    expect(plan.unplaced).toEqual([2]); // the Before you go day is never on the map, nor listed
    expect(plan.names["3"]).toEqual([{ name: "Mosswater", approximate: true }]);
  });

  it("leaves out a point that is missing or out of range (a day without a location has no pin)", () => {
    const bad = [{ name: "Nowhere", lat: 120, lng: 10, approximate: false }];
    const plan = builderMapPlan([day(1, bad), { ...day(2), map_points: null }, day(3, [JUNIPER])]);
    expect(plan.stops.map((s) => s.label)).toEqual(["3"]);
    expect(plan.unplaced).toEqual([1, 2]);
    expect(builderMapPlan([]).stops).toEqual([]);
  });
});

describe("builderMapColors", () => {
  it("paints with the theme's accent, falling back when a token is not a plain colour", () => {
    const vars: Record<string, string> = { "--aa-accent": " #1f6f8b ", "--aa-on-accent": "var(--x)" };
    const c = builderMapColors((v) => vars[v]);
    expect(c.line).toBe("#1f6f8b");
    expect(c.stop).toBe("#1f6f8b");
    expect(c.label).toMatch(/^#/); // the fallback
    expect(builderMapColors(() => null).line).toMatch(/^#/);
    expect(builderMapColors(() => "rgb(1, 2, 3)").line).toBe("rgb(1, 2, 3)");
  });
});

describe("builderMapToken", () => {
  it("takes a public token from the runtime config and refuses anything else", () => {
    expect(MAPBOX_TOKEN_ENV).toBe("MAPBOX_PUBLIC_TOKEN");
    expect(builderMapToken({ [MAPBOX_TOKEN_ENV]: " pk.example-public.fake-part " })).toBe("pk.example-public.fake-part");
    expect(builderMapToken({ [MAPBOX_TOKEN_ENV]: "sk.example-secret.fake-part" })).toBe(""); // never to the browser
    expect(builderMapToken({ [MAPBOX_TOKEN_ENV]: "pk.no-second-part" })).toBe("");
    expect(builderMapToken({ [MAPBOX_TOKEN_ENV]: "" })).toBe("");
    expect(builderMapToken({})).toBe("");
  });
});

describe("builder map messages", () => {
  it("exist in English and Vietnamese and never name an issue id", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("builderMap."));
    expect(keys.length).toBeGreaterThanOrEqual(9);
    for (const k of keys) {
      expect(MESSAGES.vi[k as keyof typeof MESSAGES.vi], k).toBeTruthy();
      expect(MESSAGES.en[k as keyof typeof MESSAGES.en]).not.toMatch(/\bAA-\d+/);
    }
  });
});

describe("the shared map engine copy", () => {
  const check = path.join(WEB, "lib/map-engine/check-map-engine.mjs");
  it("is byte-identical to its MANIFEST.json (edit AA-TripPlanner-Web, then sync)", () => {
    expect(execFileSync(process.execPath, [check], { encoding: "utf8" })).toMatch(/intact/);
  });

  it("is the only way to the map: no TripPlanner app import, no direct mapbox-gl import", () => {
    const out = execFileSync(process.execPath, [check, "--imports", WEB, "--engine", "lib/map-engine"], { encoding: "utf8" });
    expect(out).toMatch(/^ok/);
  });
});
