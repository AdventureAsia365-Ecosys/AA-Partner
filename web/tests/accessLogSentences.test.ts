// AA-1129 (S285 live check): every access log line is a plain sentence in English and Vietnamese. The codes are the
// ones the API writes (lib/auditCodes.json, kept in step with the API code by api/scripts/audit_codes.py and the API
// unit test test_aa1129_audit_codes_catalogue.py): each one has words, and no line shows an id, a raw code or a raw
// field list ("ops read the agency bab16039", "1 items shown", "viewed a list: message · from, subject, body").
import { describe, expect, it } from "vitest";
import { type AuditEventView, article, eventText } from "@/lib/accessLog";
import CODES from "@/lib/auditCodes.json";
import { type Lang, MESSAGES } from "@/lib/i18n";

const ID = "f0a838d8-1c2d-4e5f-8a9b-0c1d2e3f4a5b";
const OTHER = "bab16039-aaaa-4bbb-8ccc-0d1e2f3a4b5c";
const MEMBER = "9c1f2e3d-4b5a-4c6d-8e7f-a0b1c2d3e4f5";
const LANGS: readonly Lang[] = ["en", "vi"];
const HEX_ID = /[0-9a-f]{8}/;

function event(over: Partial<AuditEventView> = {}): AuditEventView {
  return {
    event_id: ID,
    at: "2026-11-19T02:53:00+00:00",
    actor_kind: "user",
    actor_group: "user",
    actor_ref: `member:${MEMBER}`,
    member_id: MEMBER,
    member_role: "owner",
    action: "view",
    outcome: "allowed",
    object_kind: "trip",
    object_id: ID,
    fields: [],
    reason: null,
    object_ids: [],
    ...over,
  };
}

function has(lang: Lang, key: string): boolean {
  return Boolean((MESSAGES[lang] as Record<string, string>)[key]);
}

/** A line is words only: no id, no snake_case code, no placeholder left, no double spaces. */
function expectPlain(line: string, what: string) {
  expect(line, what).not.toMatch(HEX_ID);
  expect(line, what).not.toMatch(/[a-z]_[a-z]/);
  expect(line, what).not.toMatch(/[{}]/);
  expect(line, what).not.toMatch(/ {2}/);
  expect(line.trim(), what).toBe(line);
}

describe("every audit code the API writes has words (both languages)", () => {
  it("actions: a whole sentence or a verb", () => {
    for (const lang of LANGS)
      for (const a of CODES.actions) expect(has(lang, `log.say.${a}`) || has(lang, `log.act.${a}`), `${lang} ${a}`).toBe(true);
  });

  it("objects, details and reasons", () => {
    for (const lang of LANGS) {
      for (const k of CODES.object_kinds) expect(has(lang, `log.obj.${k}`), `${lang} object ${k}`).toBe(true);
      for (const f of CODES.fields) expect(has(lang, `field.${f.replace(/_enc$/, "")}`), `${lang} field ${f}`).toBe(true);
      for (const r of CODES.reasons) expect(has(lang, `why.${r}`), `${lang} reason ${r}`).toBe(true);
    }
  });
});

describe("every event renders a sentence without ids or raw codes", () => {
  it("each action, on one object, allowed and refused", () => {
    for (const lang of LANGS)
      for (const action of CODES.actions)
        for (const outcome of ["allowed", "denied"] as const) {
          const line = eventText(event({ action, outcome, object_id: OTHER }), lang);
          expectPlain(line, `${lang} ${action}`);
          // The old fallback printed the code itself first ("sales partner list sales partner").
          expect(line.startsWith(`${action.replace(/_/g, " ")} `), `${lang} ${action}`).toBe(false);
        }
  });

  it("each object kind, opened one and viewed as a list", () => {
    for (const lang of LANGS)
      for (const kind of CODES.object_kinds) {
        expectPlain(eventText(event({ object_kind: kind, object_id: OTHER }), lang), `${lang} view ${kind}`);
        expectPlain(eventText(event({ action: "list_view", object_kind: kind, object_id: null, object_ids: [ID, OTHER] }), lang), `${lang} list ${kind}`);
      }
  });

  it("every detail and every reason", () => {
    for (const lang of LANGS) {
      expectPlain(eventText(event({ action: "reveal", fields: CODES.fields }), lang), `${lang} fields`);
      for (const reason of CODES.reasons) expectPlain(eventText(event({ reason }), lang), `${lang} reason ${reason}`);
    }
  });

  it("codes the web has no words for (a newer API) are worded generically, never printed", () => {
    const line = eventText(
      event({ action: "brand_new_action", object_kind: "brand_new_kind", fields: ["brand_new_field", "email_enc"], reason: "brand_new_reason" }),
      "en",
    );
    expect(line).toBe("worked on a record · E-mail, 1 other detail");
    expect(eventText(event({ action: "list_view", object_kind: "brand_new_kind", object_id: null }), "vi")).toBe("đã xem một danh sách");
    for (const lang of LANGS) expectPlain(eventText(event({ action: "brand_new_action", object_kind: "brand_new_kind", fields: ["a_b", "c_d"] }), lang), lang);
  });
});

describe("the lines of the S285 live check", () => {
  it("AA operations reading the agency names no id", () => {
    const ops = event({ actor_kind: "aa_ops", actor_group: "aa_ops", actor_ref: "ops:staff", member_id: null, member_role: null });
    expect(eventText({ ...ops, action: "ops_read", object_kind: "tenant", object_id: OTHER, reason: "settings" }, "en")).toBe(
      "viewed the agency · reason: settings",
    );
    expect(eventText({ ...ops, action: "ops_read", object_kind: "tenant", object_id: OTHER, reason: "settings" }, "vi")).toBe(
      "đã xem đại lý · lý do: cài đặt",
    );
  });

  it("an opened trip is 'a trip' (the line links to it)", () => {
    expect(eventText(event(), "en")).toBe("opened a trip");
    expect(eventText(event(), "vi")).toBe("đã mở chuyến đi");
  });

  it("one item is singular in English; Vietnamese has no plural", () => {
    expect(eventText(event({ action: "list_view", object_id: null, object_ids: [ID] }), "en")).toBe("viewed the trip list · 1 item shown");
    expect(eventText(event({ action: "list_view", object_id: null, object_ids: [ID, OTHER] }), "en")).toBe("viewed the trip list · 2 items shown");
    expect(eventText(event({ action: "list_view", object_id: null, object_ids: [ID] }), "vi")).toBe("đã xem danh sách chuyến đi · hiển thị 1 mục");
  });

  it("list lines are sentences with labelled details", () => {
    expect(eventText(event({ action: "sales_partner_list", object_kind: "sales_partner", object_id: null }), "en")).toBe("viewed the sales partners");
    expect(eventText(event({ action: "sales_partner_list", object_kind: "sales_partner", object_id: null }), "vi")).toBe(
      "đã xem danh sách đối tác bán hàng",
    );
    const inbox = event({ action: "list_view", object_kind: "message", object_id: null, fields: ["from", "subject", "body"] });
    expect(eventText(inbox, "en")).toBe("viewed the message list · sender, subject, message text");
    expect(eventText(inbox, "vi")).toBe("đã xem danh sách tin nhắn · người gửi, tiêu đề, nội dung tin nhắn");
  });

  it("English articles", () => {
    expect(article("trip")).toBe("a");
    expect(article("invoice")).toBe("an");
    expect(article("AI draft")).toBe("an");
    expect(article("AA tour")).toBe("an");
    expect(article("SMS and WhatsApp connection")).toBe("an");
    expect(article("supplier bill")).toBe("a");
  });
});
