// AA-943 (CRM-15) web, pure parts: the BFF allow-list lines of the stage engine, the move / gate / bulk helpers, the
// planning form's R31 branches and request body, the call-slot helpers and the EN/VI strings.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import { callSlotRules, fieldOrder, firstBadField, slotOptions } from "@/lib/intakeForms";
import {
  bulkScope,
  CANCEL_REASONS,
  canMove,
  gateOf,
  gateRefusal,
  moveErrorKey,
  MOVES,
  OVERRIDE_REASONS,
  quietDays,
} from "@/lib/pipeline";
import { planMessages } from "@/lib/planMessages";
import { answerBody, firstErrorPage, knownAnswers, LINK_TOKEN, OPTION_PREFIX, toggleCode, visibleFieldNames, visiblePages } from "@/lib/planningForm";
import { PAGES } from "./stage2Fixture";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

describe("BFF allow-list (AA-943 lines)", () => {
  it("forwards each stage-engine route, tenant-bound", () => {
    const lines: [string, string][] = [
      ["POST", `trips/${ID}/stage/undo`],
      ["POST", `trips/${ID}/cancel`],
      ["POST", "pipeline/bulk-stage"],
      ["POST", "pipeline/bulk-stage/undo"],
      ["GET", `trips/${ID}/stage2`],
      ["PUT", `trips/${ID}/stage2`],
      ["POST", `trips/${ID}/stage2/send`],
      ["GET", "settings/pipeline"],
      ["PUT", "settings/pipeline"],
    ];
    for (const [method, path] of lines) {
      const route = matchBff(method, path);
      expect(route?.card, `${method} ${path}`).toBe("AA-943");
      expect(route?.tenant).toBe(true);
    }
  });

  it("forwards nothing broader", () => {
    expect(matchBff("GET", `trips/${ID}/stage/undo`)).toBeNull();
    expect(matchBff("POST", `trips/${ID}/stage2`)).toBeNull();
    expect(matchBff("PUT", `trips/${ID}/stage2/send`)).toBeNull();
    expect(matchBff("GET", `trips/${ID}/stage2/send`)).toBeNull();
    expect(matchBff("DELETE", "settings/pipeline")).toBeNull();
    expect(matchBff("PATCH", "settings/pipeline")).toBeNull();
    expect(matchBff("GET", "pipeline/bulk-stage")).toBeNull();
    expect(matchBff("POST", "pipeline/bulk-stage/run")).toBeNull();
    expect(matchBff("POST", "trips/not-a-uuid/cancel")).toBeNull();
    expect(matchBff("POST", `trips/${ID}/cancel/now`)).toBeNull();
    expect(matchBff("POST", `dispatch/${ID}/undo`)).toBeNull();
    expect(matchBff("GET", "settings")).toBeNull();
  });
});

describe("stage engine helpers", () => {
  it("mirrors the crm-012 edges and gates", () => {
    expect(MOVES.qualifying).toEqual(["inquiry", "proposal", "lost"]);
    expect(Object.values(MOVES).flat()).not.toContain("cancelled"); // the cancel flow only
    expect(canMove("travelling", "completed")).toBe(true);
    expect(canMove("completed", "travelling")).toBe(false);
    expect(gateOf("qualifying", "proposal")).toBe("stage2");
    expect(gateOf("proposal", "booked")).toBe("booking");
    expect(gateOf("booked", "pre_departure")).toBe("stage3");
    expect(gateOf("inquiry", "qualifying")).toBeNull();
  });

  it("reads a gate refusal; other refusals are not gates", () => {
    const r = gateRefusal({ detail: "stage2_incomplete", s2: 60, missing: ["occasion", 3], override_reasons: ["client_in_hurry", "made_up"] }, "qualifying", "proposal");
    expect(r).toEqual({ detail: "stage2_incomplete", gate: "stage2", s2: 60, missing: ["occasion"], override_reasons: ["client_in_hurry"] });
    const unmet = gateRefusal({ detail: "gate_unmet", gate: "booking" }, "proposal", "booked");
    expect(unmet?.override_reasons).toEqual(["manual_booking"]);
    expect(gateRefusal({ detail: "gate_unmet" }, "proposal", "booked")?.gate).toBe("booking"); // the edge names it
    expect(gateRefusal({ detail: "transition_not_allowed" }, "inquiry", "proposal")).toBeNull();
    expect(gateRefusal(null, "qualifying", "proposal")).toBeNull();
  });

  it("words known refusals and falls back for the rest", () => {
    expect(moveErrorKey("undo_expired")).toBe("move.error.undo_expired");
    expect(moveErrorKey("use_cancel_flow")).toBe("move.error.use_cancel_flow");
    expect(moveErrorKey("something_new")).toBe("trip.error");
  });

  it("flags quiet cards from the API's flag only", () => {
    expect(quietDays({ quiet: true, quiet_days: 4 })).toBe(4);
    expect(quietDays({ quiet: false, quiet_days: 2 })).toBeNull();
    expect(quietDays({})).toBeNull();
  });

  it("moves the whole column without a filter, the cards on screen with one", () => {
    const column = { stage: "qualifying" as const, cards: [{ trip_id: "a" }, { trip_id: "b" }] };
    expect(bulkScope(column, { waiting_on: null, owner: "anyone" })).toEqual({ from_stage: "qualifying" });
    expect(bulkScope(column, { waiting_on: "client", owner: "anyone" })).toEqual({ trip_ids: ["a", "b"] });
    expect(bulkScope(column, { waiting_on: null, owner: "me" })).toEqual({ trip_ids: ["a", "b"] });
  });
});

describe("planning form (R31)", () => {
  it("shows the adventure questions only for an adventure trip", () => {
    expect(visibleFieldNames(PAGES, "relaxed", {}).has("trek_experience")).toBe(false);
    expect(visibleFieldNames(PAGES, null, {}).has("altitude_ok")).toBe(false);
    expect(visibleFieldNames(PAGES, "adventure", {}).has("trek_experience")).toBe(true);
  });

  it("shows the health page only after the consent", () => {
    expect(visiblePages(PAGES, null, {}).map((p) => p.key)).toEqual(["travel_style", "interests", "planning", "health_consent"]);
    expect(visiblePages(PAGES, null, { health_consent: true }).map((p) => p.key)).toContain("health");
    expect(visiblePages(PAGES, null, { health_consent: "true" }).map((p) => p.key)).not.toContain("health");
  });

  it("posts visible, non-empty answers only, the consent as a boolean, and the submission id", () => {
    const answers = { accommodation_tier: "comfort", altitude_ok: true, interests: [], must_do_detail: "  Ha Long  ", dietary_flags: ["vegan"], bogus: 1 };
    expect(answerBody(PAGES, "relaxed", answers, "sub-12345678")).toEqual({
      accommodation_tier: "comfort",
      must_do_detail: "Ha Long",
      health_consent: false,
      client_submission_id: "sub-12345678",
    });
    const withConsent = answerBody(PAGES, "adventure", { ...answers, health_consent: true }, "");
    expect(withConsent).toMatchObject({ altitude_ok: true, dietary_flags: ["vegan"], health_consent: true });
    expect(withConsent).not.toHaveProperty("client_submission_id");
  });

  it("finds the page of the first refused field and keeps multi-selects inside their limit", () => {
    const visible = visiblePages(PAGES, null, { health_consent: true });
    expect(firstErrorPage(visible, ["dietary_flags", "interests"])).toBe(1);
    expect(firstErrorPage(visible, ["nope"])).toBeNull();
    expect(toggleCode(["culture"], "food", 2)).toEqual(["culture", "food"]);
    expect(toggleCode(["culture", "food"], "nature", 2)).toEqual(["culture", "food"]);
    expect(toggleCode(["culture", "food"], "food", 2)).toEqual(["culture"]);
    expect(knownAnswers(PAGES, { group_pref: "private", old_field: "x", interests: [] })).toEqual({ group_pref: "private" });
  });

  it("accepts the link token shape only", () => {
    expect(LINK_TOKEN.test("a".repeat(43))).toBe(true);
    expect(LINK_TOKEN.test("a".repeat(42))).toBe(false);
    expect(LINK_TOKEN.test(`${"a".repeat(42)}/`)).toBe(false);
  });
});

describe("call slots and the inquiry form order", () => {
  it("groups the offered UTC starts by day in the viewer's zone; the value stays UTC", () => {
    const groups = slotOptions(["2026-11-02T02:00:00Z", "2026-11-02T02:30:00Z", "2026-11-03T03:00:00Z", "not a date"], "en-AU", "Asia/Ho_Chi_Minh");
    expect(groups).toHaveLength(2);
    expect(groups[0].day).toMatch(/Mon.*2.*Nov/);
    expect(groups[0].options).toEqual([
      { value: "2026-11-02T02:00:00Z", label: expect.stringMatching(/09:00/) },
      { value: "2026-11-02T02:30:00Z", label: expect.stringMatching(/09:30/) },
    ]);
  });

  it("builds the call-slot rules the API stores", () => {
    expect(callSlotRules([], 30, 14)).toEqual({});
    expect(callSlotRules([{ dow: 3, start: "14:00", end: "17:00" }, { dow: 1, start: "09:00", end: "12:00" }], 30, 14)).toEqual({
      windows: [
        { dow: 1, start: "09:00", end: "12:00" },
        { dow: 3, start: "14:00", end: "17:00" },
      ],
      duration_min: 30,
      horizon_days: 14,
    });
  });

  it("orders refused fields by the API's pages", () => {
    const form = {
      fields: [{ name: "email", type: "email", required: true }, { name: "nights", type: "integer", required: false }, { name: "extra", type: "text", required: false }],
      pages: [
        { key: "trip", fields: ["nights"] },
        { key: "contact", fields: ["email"] },
      ],
    };
    expect(fieldOrder(form)).toEqual(["nights", "email", "extra"]);
    expect(firstBadField(form, ["email", "nights"])).toBe("nights");
    expect(firstBadField({ fields: form.fields }, ["nights", "email"])).toBe("email");
    expect(firstBadField(form, [])).toBeNull();
  });
});

describe("EN/VI (AA-943)", () => {
  it("every new key exists in both languages", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => /^(gate|override|cancelTrip|cancel|undo|skip|info|s1|plan|interest|avoid|pset|dow|move|blocked)\./.test(k));
    expect(keys.length).toBeGreaterThan(180);
    for (const k of keys) expect(MESSAGES.vi[k as keyof typeof MESSAGES.vi], k).toBeTruthy();
  });

  it("words every code the stage engine and the planning form send", () => {
    const en = MESSAGES.en as Record<string, string>;
    for (const r of Object.values(OVERRIDE_REASONS).flat()) expect(en[`override.${r}`], r).toBeTruthy();
    for (const r of CANCEL_REASONS) expect(en[`cancel.${r}`], r).toBeTruthy();
    for (const k of ["dispatch_cancelled", "tasks_skipped", "call_slots_cancelled", "client_links_revoked"]) expect(en[`cancelTrip.effect.${k}`]).toBeTruthy();
    for (const p of ["travel_style", "interests", "planning", "health_consent", "health"]) expect(en[`plan.page.${p}`]).toBeTruthy();
    for (const f of Object.keys(OPTION_PREFIX)) expect(en[`plan.field.${f}`], f).toBeTruthy();
    // API app/intake/stage2.py INTERESTS / AVOID
    const interests = "culture food nature history beaches wellness photography wildlife trekking cycling cooking homestay markets temples boat_trips nightlife";
    for (const c of interests.split(" ")) expect(en[`interest.${c}`], c).toBeTruthy();
    for (const c of "long_drives crowds early_starts heights spicy_food boat_trips camping nightlife".split(" ")) expect(en[`avoid.${c}`], c).toBeTruthy();
    for (const c of ["get_planning_info", "call_scheduled", "call_slot_taken", "date_rule_gate_unmet", "return_date_missing"]) expect(en[`task.${c}`], c).toBeTruthy();
    for (const c of ["bulk_move", "date_rule", "date_rule_gate_unmet"]) expect(en[`reason.${c}`], c).toBeTruthy();
  });

  it("sends the client page only the planning form's strings", () => {
    const sent = Object.keys(planMessages("en"));
    expect(sent).toContain("plan.title");
    expect(sent).toContain("interest.culture");
    expect(sent.some((k) => k.startsWith("pipeline.") || k.startsWith("trip.") || k.startsWith("field."))).toBe(false);
  });
});
