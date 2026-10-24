// AA-943 test fixture: the API's stage2.v1 definition (api/app/intake/stage2.py PAGES), options trimmed.
import type { PlanPage } from "@/lib/planningForm";

export const PAGES: PlanPage[] = [
  {
    key: "travel_style",
    fields: [
      { name: "accommodation_tier", type: "select", required: false, options: ["budget", "comfort", "premium", "luxury"] },
      { name: "group_pref", type: "select", required: false, options: ["private", "small_group"] },
      { name: "fitness_band", type: "select", required: false, options: ["up_to_2h", "2_4h"] },
      { name: "trek_experience", type: "select", required: false, options: ["none", "some", "experienced"], show_if: { field: "trip_style", in: ["adventure"] } },
      { name: "altitude_ok", type: "boolean", required: false, show_if: { field: "trip_style", in: ["adventure"] } },
    ],
  },
  {
    key: "interests",
    fields: [
      { name: "interests", type: "multi_select", required: false, options: ["culture", "food", "nature"], max_items: 2 },
      { name: "must_do_detail", type: "long_text", required: false, max_length: 1000 },
    ],
  },
  { key: "planning", fields: [{ name: "competing_quotes", type: "boolean", required: false }] },
  { key: "health_consent", fields: [{ name: "health_consent", type: "consent", required: false, purpose: "health_data" }] },
  {
    key: "health",
    show_if: { field: "health_consent", in: [true] },
    fields: [
      { name: "dietary_flags", type: "multi_select", required: false, options: ["vegan", "halal"] },
      { name: "mobility_flags", type: "multi_select", required: false, options: ["wheelchair"] },
    ],
  },
];

