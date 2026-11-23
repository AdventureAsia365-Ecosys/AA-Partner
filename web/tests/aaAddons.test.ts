// AA-1152: the AA add-on drawer tab and the planner prices panel helpers (invented places only).
import { describe, expect, it } from "vitest";
import { changedSteps, durationText, placesText, weekdaysText } from "@/lib/aaAddons";
import { drawerTabs } from "@/lib/agencyItems";
import { builderErrorKey } from "@/lib/builder";
import { t } from "@/lib/i18n";
import { plannerPricesEn, plannerPricesVi } from "@/lib/i18nPlannerPrices";

describe("AA add-on drawer", () => {
  it("offers the AA add-on tab on every block, after AA inventory", () => {
    expect(drawerTabs("activity")).toContain("addon");
    expect(drawerTabs("other").indexOf("addon")).toBe(1);
  });

  it("words the weekdays, the duration and the places", () => {
    expect(weekdaysText([1, 2, 3, 4, 5, 6, 7], "en")).toBe("Every day");
    expect(weekdaysText([6, 2, 4], "en")).toBe("Tue, Thu, Sat");
    expect(weekdaysText([7], "vi")).toBe("CN");
    expect(durationText(180)).toBe("3 h");
    expect(durationText(90)).toBe("1 h 30 min");
    expect(durationText(null)).toBeNull();
    expect(
      placesText(
        [
          { destination_id: "a", role: "operates", name: "Lantern Bay" },
          { destination_id: "b", role: "pickup", name: "Harbour Hill" },
        ],
        "Pick-up",
      ),
    ).toBe("Lantern Bay · Pick-up: Harbour Hill");
  });

  it("names the add-on refusals", () => {
    expect(t("en", builderErrorKey("aa_addon_needs_tour"))).toContain("AA tour");
    expect(t("vi", builderErrorKey("aa_addon_not_offered"))).not.toBe("");
  });
});

describe("planner prices panel", () => {
  const currencies = [
    { currency: "USD", step: "5", chosen: true, default: "1", choices: ["1", "5", "10"] },
    { currency: "VND", step: "10000", chosen: false, default: "10000", choices: ["10000", "50000"] },
  ];

  it("sends only the steps that changed, null = back to the default", () => {
    expect(changedSteps(currencies, { USD: "5", VND: "" })).toEqual({});
    expect(changedSteps(currencies, { USD: "", VND: "50000" })).toEqual({ USD: null, VND: "50000" });
    expect(changedSteps(currencies, { USD: "10" })).toEqual({ USD: "10" });
  });

  it("has every key in both languages", () => {
    expect(Object.keys(plannerPricesVi).sort()).toEqual(Object.keys(plannerPricesEn).sort());
    expect(Object.values(plannerPricesEn).join(" ")).not.toMatch(/AA-\d+/);
  });
});
