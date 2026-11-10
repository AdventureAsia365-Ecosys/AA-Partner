// AA-1067 (crm-049 / crm-050): the retail payment features on the web — the builder's payment lines and their body
// rule, the template's body rule, the policy tiers' body rule, the cancel dialog's charge body, the live-status tones,
// the client page's lines and policy, the builder's check words, the BFF allow-list lines of the new routes, and the
// copy in both languages.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ClientPlanLines, ClientPolicy, ClientScheduleRows } from "@/components/proposals/ClientPaymentLines";
import { ScheduleEditor } from "@/components/proposals/ScheduleEditor";
import { builderErrorKey, checkKey } from "@/lib/builder";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import { retailPaymentsEn, retailPaymentsVi } from "@/lib/i18nRetailPayments";
import {
  addInstalment,
  chargeBody,
  draftLines,
  draftTiers,
  lineLabel,
  liveTone,
  paymentsErrorKey,
  restPercent,
  scheduleBody,
  templateBody,
  templateLineText,
  templateRows,
  tiersBody,
  tierText,
  type ScheduleLine,
} from "@/lib/retailPayments";

const P = "11111111-1111-4111-8111-111111111111";
const tr = (key: string) => MESSAGES.en[key as keyof typeof MESSAGES.en] ?? key;
const vi = (key: string) => MESSAGES.vi[key as keyof typeof MESSAGES.vi] ?? key;

const LINES: ScheduleLine[] = [
  { kind: "deposit", seq: 1, percent: "30", due_on: null, days_before: null },
  { kind: "instalment", seq: 1, percent: "40", due_on: "2027-01-01", days_before: 60 },
  { kind: "balance", seq: 1, percent: null, due_on: "2027-01-31", days_before: null },
];

describe("payment lines", () => {
  it("words each line and builds the PATCH body", () => {
    expect(LINES.map((l) => lineLabel(l, tr))).toEqual(["Deposit", "Instalment 1", "Balance"]);
    expect(lineLabel({ kind: "instalment", seq: 2 }, vi)).toBe("Đợt 2");
    const rows = draftLines(LINES);
    expect(scheduleBody(rows)).toEqual([
      { kind: "deposit", percent: "30" },
      { kind: "instalment", percent: "40", days_before: 60 }, // still follows the template: the days go
      { kind: "balance", due_on: "2027-01-31" },
    ]);
    const more = addInstalment(rows);
    expect(more.map((r) => r.kind)).toEqual(["deposit", "instalment", "instalment", "balance"]);
    expect(restPercent(more)).toBe(20);
    expect(addInstalment([rows[0]]).map((r) => r.kind)).toEqual(["deposit", "instalment"]);
  });

  it("refuses a line it cannot send", () => {
    const rows = draftLines(LINES);
    expect(scheduleBody([])).toBeNull();
    expect(scheduleBody([rows[1], rows[0]])).toBeNull(); // the deposit first
    expect(scheduleBody([{ ...rows[0], percent: "0" }, rows[2]])).toBeNull();
    expect(scheduleBody([{ ...rows[0], percent: "30.123" }, rows[2]])).toBeNull();
    expect(scheduleBody([rows[0], { ...rows[2], due_on: "31/01/2027" }])).toBeNull();
    expect(scheduleBody([{ ...rows[0], percent: "12,5" }, rows[2]])).toEqual([
      { kind: "deposit", percent: "12.5" },
      { kind: "balance", due_on: "2027-01-31" },
    ]);
  });

  it("builds the template body and words its lines", () => {
    const tpl = [
      { kind: "deposit" as const, percent: "30", due: { anchor: "accept" as const } },
      { kind: "instalment" as const, percent: "40", due: { anchor: "before_start" as const, days: 60 } },
      { kind: "balance" as const, due: { anchor: "before_start" as const, days: 30 } },
    ];
    expect(templateBody(templateRows(tpl))).toEqual(tpl);
    expect(tpl.map((l) => templateLineText(l, tr))).toEqual([
      "30% when the client accepts",
      "40% 60 days before departure",
      "The rest 30 days before departure",
    ]);
    expect(templateBody([{ kind: "balance", percent: "", days: "30" }])).toBeNull();
    expect(templateBody([{ kind: "deposit", percent: "30", days: "" }, { kind: "balance", percent: "", days: "x" }])).toBeNull();
    expect(templateBody([{ kind: "deposit", percent: "30", days: "" }, { kind: "balance", percent: "", days: "731" }])).toBeNull();
  });

  it("gives each live status a tone", () => {
    expect(["due", "partial", "overdue", "paid", "void"].map(liveTone)).toEqual(["info", "warning", "danger", "success", "neutral"]);
  });
});

describe("cancellation policy and charge", () => {
  it("checks the tiers and words them", () => {
    const rows = draftTiers([
      { days_before_min: 31, charge_pct: "10", min_charge_amount: null },
      { days_before_min: 0, charge_pct: "100", min_charge_amount: "50" },
    ]);
    expect(tiersBody([rows[1], rows[0]])).toEqual([
      { days_before_min: 31, charge_pct: "10" },
      { days_before_min: 0, charge_pct: "100", min_charge_amount: "50" },
    ]);
    expect(tiersBody([rows[0]])).toBeNull(); // no 0-day line
    expect(tiersBody([rows[1], { ...rows[1], charge_pct: "5" }])).toBeNull(); // a day count twice
    expect(tiersBody([{ ...rows[1], charge_pct: "101" }])).toBeNull();
    expect(tiersBody([{ ...rows[1], min_charge_amount: "0" }])).toBeNull();
    expect(tierText({ days_before_min: 15, charge_pct: "25", min_charge_amount: null }, tr)).toBe("15 days or more before departure: 25%");
  });

  it("builds the cancel body's charge", () => {
    expect(chargeBody("policy", "", "")).toBeUndefined();
    expect(chargeBody("waive", "", " Family emergency ")).toEqual({ mode: "waive", reason: "Family emergency" });
    expect(chargeBody("override", "1,200.50", "Rebooked")).toEqual({ mode: "override", amount: "1200.50", reason: "Rebooked" });
    expect(chargeBody("override", "12.345", "Rebooked")).toBeNull();
    expect(chargeBody("waive", "", "  ")).toBeNull();
  });

  it("maps the refusal codes to words", () => {
    for (const code of ["invalid_schedule", "invalid_tiers", "cancel_charge_owner_only", "policy_not_live", "charge_reason_required"]) {
      const key = paymentsErrorKey(code);
      expect(key && tr(key)).not.toBe(key);
    }
    expect(paymentsErrorKey("something_else")).toBeNull();
    expect(builderErrorKey("invalid_schedule")).toBe("payments.err.invalid_schedule");
    for (const code of ["schedule_dates_required", "schedule_dates_order", "schedule_after_start", "schedule_amount_zero"]) {
      expect(checkKey(code)).toBe(`builder.check.${code}`);
    }
  });
});

describe("pages", () => {
  it("renders the builder's lines with amounts and the policy", () => {
    const html = renderToStaticMarkup(
      createElement(ScheduleEditor, {
        proposalId: P,
        lines: LINES,
        amounts: LINES.map((l, i) => ({ ...l, amount: ["300.00", "400.00", "300.00"][i] })),
        policy: { policy_id: P, name: "Standard", version: 2, currency: null, tiers: [], text: null },
        draft: false,
        canMoney: true,
        busy: false,
        locale: "en-AU",
        money: (v) => `${v} USD`,
        tr,
        write: async () => true,
        onMessage: () => undefined,
      }),
    );
    expect(html).toContain("Instalment 1: 400.00 USD");
    expect(html).toContain("Deposit: 300.00 USD · When the client accepts");
    expect(html).toContain("Cancellation policy: Standard (version 2)");
    const draft = renderToStaticMarkup(
      createElement(ScheduleEditor, {
        proposalId: P, lines: LINES, amounts: undefined, policy: null, draft: true, canMoney: true, busy: false,
        locale: "en-AU", money: (v) => String(v), tr, write: async () => true, onMessage: () => undefined,
      }),
    );
    expect(draft).toContain('data-testid="builder-schedule-line"');
    expect(draft).toContain("The rest: 30%");
    expect(draft).toContain("Add an instalment");
  });

  it("renders the client's lines, rows and policy", () => {
    const t = (k: string) => MESSAGES.en[k as keyof typeof MESSAGES.en] ?? k;
    const plan = renderToStaticMarkup(
      createElement(ClientPlanLines, {
        lines: [
          { kind: "deposit", seq: 1, amount: "300.00", due_on: null },
          { kind: "instalment", seq: 1, amount: "400.00", due_on: "2027-01-01" },
        ],
        currency: "USD",
        locale: "en-AU",
        t,
      }),
    );
    expect(plan).toContain("Instalment 1");
    expect(plan).toContain("when you accept");
    const rows = renderToStaticMarkup(
      createElement(ClientScheduleRows, {
        rows: [{ kind: "instalment", seq: 2, amount: "250.00", currency: "USD", due_on: "2027-01-01", status: "due" }],
        locale: "en-AU",
        t,
      }),
    );
    expect(rows).toContain("Instalment 2");
    const policy = renderToStaticMarkup(
      createElement(ClientPolicy, {
        policy: { policy_id: P, name: "Standard", version: 1, currency: null, tiers: [], text: { en: "English table", vi: "Bảng tiếng Việt" } },
        locale: "vi-VN",
        t,
      }),
    );
    expect(policy).toContain("Bảng tiếng Việt");
    expect(renderToStaticMarkup(createElement(ClientPolicy, { policy: null, locale: "en-AU", t }))).toBe("");
  });
});

describe("BFF and copy", () => {
  it("allows the new routes and nothing else", () => {
    expect(matchBff("GET", "settings/payment-schedule")).not.toBeNull();
    expect(matchBff("PUT", "settings/payment-schedule")).not.toBeNull();
    expect(matchBff("GET", "cancellation-policies")).not.toBeNull();
    expect(matchBff("POST", "cancellation-policies")).not.toBeNull();
    expect(matchBff("PATCH", `cancellation-policies/${P}`)).not.toBeNull();
    expect(matchBff("DELETE", `cancellation-policies/${P}`)).not.toBeNull();
    expect(matchBff("POST", `cancellation-policies/${P}/publish`)).not.toBeNull();
    expect(matchBff("POST", `cancellation-policies/${P}/delete`)).toBeNull();
    expect(matchBff("GET", `trips/${P}/cancellation-charge`)).not.toBeNull();
    expect(matchBff("DELETE", "settings/payment-schedule")).toBeNull();
  });

  it("has every key in both languages, and never an issue id", () => {
    expect(Object.keys(retailPaymentsVi).sort()).toEqual(Object.keys(retailPaymentsEn).sort());
    for (const text of [...Object.values(retailPaymentsEn), ...Object.values(retailPaymentsVi)]) {
      expect(text).not.toMatch(/AA-\d+/);
      expect(text.trim()).not.toBe("");
    }
  });
});
