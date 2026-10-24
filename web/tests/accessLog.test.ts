// AA-940 (CRM-12): the access log helpers (filters from the URL, BFF paths, EN/VI event lines, links, file name), the
// BFF allow-list lines, and the EN/VI strings of every action and actor group.
import { describe, expect, it } from "vitest";
import {
  ACTIONS,
  ACTOR_GROUPS,
  type AuditEventView,
  actorText,
  cleanFilter,
  codeLabel,
  eventText,
  eventsPath,
  exportFilename,
  exportPath,
  objectHref,
  shortId,
  whenText,
} from "@/lib/accessLog";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const MEMBER = "1f8fad5b-d9cb-469f-a165-70867728950e";

function event(over: Partial<AuditEventView> = {}): AuditEventView {
  return {
    event_id: ID,
    at: "2026-10-20T07:02:00+00:00",
    actor_kind: "user",
    actor_group: "user",
    actor_ref: `member:${MEMBER}`,
    member_id: MEMBER,
    member_role: "agent",
    action: "reveal",
    outcome: "allowed",
    object_kind: "traveler",
    object_id: ID,
    fields: ["passport_no_enc"],
    reason: "prepare_booking",
    object_ids: [],
    ...over,
  };
}

describe("access log filters", () => {
  it("keeps only well-formed values from the URL", () => {
    expect(cleanFilter({ actor: "ai", action: "reveal", outcome: "denied", from: "2026-10-01", to: "2026-10-24" })).toEqual({
      actor: "ai",
      action: "reveal",
      outcome: "denied",
      from: "2026-10-01",
      to: "2026-10-24",
    });
    expect(cleanFilter({ actor: "everyone", action: "DROP TABLE", outcome: "maybe", from: "1 Oct", to: ["2026-10-01"] })).toEqual({});
  });

  it("builds the BFF paths the allow-list accepts", () => {
    const list = eventsPath({ actor: "aa_ops", from: "2026-10-01" }, "abc");
    expect(list).toBe("audit/events?actor=aa_ops&from=2026-10-01&cursor=abc&limit=50");
    expect(exportPath({ outcome: "denied" }, "csv")).toBe("audit/events/export?outcome=denied&format=csv");
    expect(matchBff("GET", list.split("?")[0])?.card).toBe("AA-940");
    expect(matchBff("GET", "audit/events/export")?.tenant).toBe(true);
    expect(matchBff("POST", "audit/sign-in")).toBeNull(); // server side only (/auth/agency), no browser route
    expect(matchBff("DELETE", "audit/events")).toBeNull();
    expect(matchBff("POST", "audit/events")).toBeNull();
    expect(exportFilename("jsonl", new Date("2026-10-24T10:00:00Z"))).toBe("access-log-20261024.jsonl");
  });
});

describe("access log lines", () => {
  it("say who did what, in English and Vietnamese, with ids and codes only", () => {
    const e = event();
    expect(actorText(e, "en")).toBe("Agent 1f8fad5b");
    expect(eventText(e, "en")).toBe("revealed client 0f8fad5b · Passport number · reason: Prepare a booking");
    expect(eventText(e, "vi")).toContain("Số hộ chiếu");
    expect(actorText(event({ member_role: null }), "en")).toBe("Former member 1f8fad5b");
    const ai = event({ actor_kind: "ai_agent", actor_group: "ai", actor_ref: "ai:crm_draft", member_id: null, member_role: null });
    expect(actorText(ai, "en")).toBe("AI (crm_draft)");
    expect(actorText(event({ actor_group: "system", member_id: null }), "vi")).toBe("Hệ thống");
    const list = event({ action: "list_view", object_kind: "pipeline", object_id: null, fields: ["display_name"], reason: "board", object_ids: [ID, MEMBER] });
    expect(eventText(list, "en")).toBe("viewed the list pipeline board · Name shown in lists · reason: board · 2 items shown");
    const key = event({ action: "key_create", object_kind: "tenant_key", object_id: "vault:v1", fields: [], reason: "purpose_vault" });
    expect(eventText(key, "en")).toBe("created encryption key version vault:v1 · reason: purpose vault");
    expect(codeLabel("en", "log.act", "brand_new_code")).toBe("brand new code");
    expect(codeLabel("en", "log.act", null)).toBe("");
    expect(shortId(null)).toBe("");
  });

  it("link clients and trips, and write the date with the month spelled", () => {
    expect(objectHref(event())).toBe(`/clients/${ID}`);
    expect(objectHref(event({ object_kind: "trip" }))).toBe(`/pipeline/${ID}`);
    expect(objectHref(event({ object_kind: "document" }))).toBeNull();
    expect(objectHref(event({ object_id: "vault:v1" }))).toBeNull();
    expect(whenText("2026-10-20T07:02:00+00:00", "en-AU")).toMatch(/20 Oct/);
    expect(whenText("not a date", "en-AU")).toBe("");
  });

  it("have a label for every action and actor group in both languages", () => {
    for (const lang of ["en", "vi"] as const) {
      for (const a of ACTIONS) expect(MESSAGES[lang][`log.act.${a}` as keyof (typeof MESSAGES)["en"]], `${lang} ${a}`).toBeTruthy();
      for (const g of ACTOR_GROUPS) expect(MESSAGES[lang][`log.actor.${g}` as keyof (typeof MESSAGES)["en"]], `${lang} ${g}`).toBeTruthy();
    }
  });
});
