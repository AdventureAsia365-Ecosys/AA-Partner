// AA-949: the builder helpers (lib/builder.ts) and the BFF lines of card AA-949 (lib/bff.ts).
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import {
  builderErrorKey,
  checkKey,
  marginPct,
  moneyInput,
  offsetLabel,
  optionLabel,
  roomsBody,
  roomsSummary,
  versionConflict,
  type BuilderView,
} from "@/lib/builder";
import { MESSAGES } from "@/lib/i18n";

const P = "00000000-0000-4000-8000-0000000000b1";
const X = "00000000-0000-4000-8000-0000000000b2";

function view(): BuilderView {
  return {
    proposal_id: P,
    trip_id: X,
    trip_stage: "proposal",
    version: 2,
    status: "draft",
    kind: "builder",
    edit_seq: 4,
    selection_seq: 0,
    currency: "USD",
    price_visibility: "total_with_deltas",
    room_config: [{ type: "family", adults: 2, children: 1 }],
    deposit_pct: "30.00",
    starts_on: "2026-12-01",
    balance_due_on: null,
    terms_doc_id: null,
    intro: "",
    price_hash: null,
    published_at: null,
    based_on: null,
    days: [
      {
        proposal_day_id: "d1",
        day_no: 1,
        before_you_go: false,
        date: "2026-12-01",
        title: "Arrive",
        place: null,
        tz: "Asia/Kathmandu",
        utc_offset: "+05:45",
        notes: null,
        blocks: [
          {
            option_block_id: "b1",
            title: "Lodging",
            type: "lodging",
            sort_order: 0,
            client_may_swap: true,
            default_option_id: "o1",
            options: [
              { option_id: "o1", source: "agency", title: "Homestay", tier: null, agency_supplier_id: null, sell_amount: "200.00", sell_delta: "0.00", pay_on_arrival: false, choose_later_allowed: false, availability: "agency", availability_checked_at: null, is_default: true, picked: false },
            ],
          },
        ],
      },
    ],
    rules: [],
    picks: [],
    totals: { currency: "USD", sell_total: "200.00", pay_on_arrival_total: "0.00", deposit: "60.00", balance: "140.00" },
    checks: { publishable: true, blocking: [], warnings: [] },
    stage2: { accommodation_tier: null, group_pref: null, pace: null, party_adults: 2, party_children: 1 },
  };
}

describe("builder helpers", () => {
  it("label a day's offset (card E: Nepal is UTC+05:45)", () => {
    expect(offsetLabel("+05:45")).toBe("UTC+05:45");
    expect(offsetLabel("-08:00")).toBe("UTC-08:00");
    expect(offsetLabel("junk")).toBe("UTC");
  });

  it("read a version conflict from a 409", () => {
    expect(versionConflict(409, "version_conflict", { detail: "version_conflict", reason: "edited", current_seq: 5 })).toMatchObject({ reason: "edited", current_seq: 5 });
    const picks = versionConflict(409, "version_conflict", { reason: "client_selected", selection_seq: 2, picks: [{ block: "Lodging", option: "Lodge", chosen_by: "client", at: null }] });
    expect(picks?.selection_seq).toBe(2);
    expect(versionConflict(409, "option_conflict", { reason: "edited" })).toBeNull();
    expect(versionConflict(422, "version_conflict", { reason: "edited" })).toBeNull();
    expect(versionConflict(409, "version_conflict", null)).toBeNull();
  });

  it("compute the margin share and parse typed money", () => {
    expect(marginPct("18680", "3432")).toBe("18.4");
    expect(marginPct("0", "0")).toBeNull();
    expect(marginPct("100", null)).toBeNull();
    expect(moneyInput("1,500.5")).toBe("1500.50");
    expect(moneyInput("")).toBeNull();
    expect(moneyInput("-1")).toBeUndefined();
    expect(moneyInput("1.234")).toBeUndefined();
  });

  it("check rooms and summarise them", () => {
    expect(roomsBody([{ type: "family", adults: 2, children: 2 }]).ok).toBe(true);
    expect(roomsBody([]).ok).toBe(false);
    expect(roomsBody([{ type: "double", adults: 0, children: 0 }]).ok).toBe(false);
    expect(roomsBody([{ type: "double", adults: 7, children: 0 }]).ok).toBe(false);
    const words = { single: "single", double: "double", twin: "twin", triple: "triple", family: "family" } as const;
    expect(roomsSummary([{ type: "family", adults: 2, children: 1 }, { type: "single", adults: 1, children: 0 }], words)).toBe("family (2A 1C), single (1A)");
  });

  it("name options for rule lines", () => {
    expect(optionLabel(view(), "o1", "Day")).toBe("Day 1 · Lodging · Homestay");
    expect(optionLabel(view(), "nope", "Day")).toBe("?");
  });

  it("word every refusal and check in both languages", () => {
    for (const code of ["version_conflict", "option_conflict", "aa_sold_out", "booking_unavailable", "role_forbidden", "something_new"]) {
      const key = builderErrorKey(code);
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    for (const code of ["no_options", "aa_prices_stale", "below_from_price", "net_missing", "unknown"]) {
      expect(MESSAGES.vi[checkKey(code)]).toBeTruthy();
    }
  });
});

describe("BFF lines of card AA-949", () => {
  it("forward the builder routes only", () => {
    const allowed: [string, string][] = [
      ["POST", `trips/${X}/builder`],
      ["GET", `proposals/${P}/builder`],
      ["PATCH", `proposals/${P}/builder`],
      ["POST", `proposals/${P}/days`],
      ["PUT", `proposals/${P}/days/order`],
      ["PATCH", `proposals/${P}/days/${X}`],
      ["DELETE", `proposals/${P}/days/${X}`],
      ["POST", `proposals/${P}/days/${X}/blocks`],
      ["PATCH", `proposals/${P}/blocks/${X}`],
      ["DELETE", `proposals/${P}/blocks/${X}`],
      ["POST", `proposals/${P}/blocks/${X}/options`],
      ["POST", `proposals/${P}/blocks/${X}/aa-options`],
      ["PATCH", `proposals/${P}/options/${X}`],
      ["DELETE", `proposals/${P}/options/${X}`],
      ["POST", `proposals/${P}/rules`],
      ["DELETE", `proposals/${P}/rules/${X}`],
      ["POST", `proposals/${P}/aa-refresh`],
      ["POST", `proposals/${P}/publish`],
    ];
    for (const [method, path] of allowed) expect(matchBff(method, path)?.card, `${method} ${path}`).toBe("AA-949");
    for (const [method, path] of [
      ["DELETE", `proposals/${P}/builder`],
      ["POST", `proposals/${P}/selections`],
      ["PATCH", `proposals/${P}/rules/${X}`],
      ["GET", `proposals/${P}/days`],
      ["POST", `proposals/${P}/blocks/${X}/options/x`],
    ] as const) {
      expect(matchBff(method, path)?.card, `${method} ${path}`).not.toBe("AA-949");
    }
  });
});
