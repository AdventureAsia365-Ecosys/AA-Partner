// AA-944 (CRM-16): the ledger helpers (money formatting, the frozen FX reading, refusal wording, the money body) and
// the BFF allow-list lines of the ledger routes.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import { errorKey, formatMoney, fxReading, missingCodes, moneyBody, seesMargin } from "@/lib/ledger";

const ID = "00000000-0000-4000-8000-0000000000e1";

describe("ledger helpers", () => {
  it("formats amounts in the locale, without decimals for VND", () => {
    expect(formatMoney("2540000.00", "VND", "en-AU")).toBe("2,540,000 VND");
    expect(formatMoney("120", "USD", "en-AU")).toBe("120.00 USD");
    expect(formatMoney(undefined, "USD", "en-AU")).toBe("—");
    expect(formatMoney("x", "USD", "en-AU")).toBe("x USD");
  });

  it("reads a frozen rate and nothing while the line is planning", () => {
    const fx = { rate: "0.00004", rate_date: "2026-10-24", source: "platform_daily" as const, stale_days: 0 };
    expect(fxReading({ amount: "1", currency: "VND", base_currency: "USD", amount_base: "0.00", fx })).toBe("1 VND = 0.00004 USD");
    expect(fxReading({ amount: "1", currency: "VND" })).toBeNull();
    expect(fxReading({ amount: "1", currency: "USD", base_currency: "USD", fx })).toBeNull();
    expect(fxReading(null)).toBeNull();
  });

  it("only owner and agent see margins", () => {
    expect(["owner", "agent", "assistant", "x"].map(seesMargin)).toEqual([true, true, false, false]);
  });

  it("words every refusal code in both languages", () => {
    for (const code of ["line_incomplete", "duplicate_confirmation_no", "aa_line_read_only", "fx_rate_missing", "supplier_in_use"]) {
      const key = errorKey(code) as keyof typeof MESSAGES.en;
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    expect(errorKey("teapot")).toBe("ledger.error.other");
    expect(missingCodes({ missing: ["cost", 3, "sell"] })).toEqual(["cost", "sell"]);
    expect(missingCodes(null)).toEqual([]);
    for (const code of ["confirmation_no", "cost", "sell", "commission"]) {
      expect(MESSAGES.vi[`ledger.missing.${code}` as keyof typeof MESSAGES.en]).toBeTruthy();
    }
  });

  it("builds a money body from form text and refuses bad input", () => {
    expect(moneyBody({ cost: "2,540,000", sell: "", commission_expected: "10.5" }, "VND")).toEqual({
      cost: { amount: "2540000", currency: "VND" },
      commission_expected: { amount: "10.5", currency: "VND" },
    });
    expect(moneyBody({ cost: "1.234" }, "USD")).toBeNull();
    expect(moneyBody({ cost: "1" }, "usd")).toBeNull();
    expect(moneyBody({}, "USD")).toBeNull();
  });
});

describe("BFF allow-list (AA-944)", () => {
  it.each([
    ["GET", "suppliers"],
    ["POST", "suppliers"],
    ["PATCH", `suppliers/${ID}`],
    ["DELETE", `suppliers/${ID}`],
    ["POST", `suppliers/${ID}/reveal`],
    ["GET", `trips/${ID}/lines`],
    ["POST", `trips/${ID}/lines`],
    ["PATCH", `lines/${ID}`],
    ["DELETE", `lines/${ID}`],
    ["POST", `lines/${ID}/confirm`],
    ["POST", `lines/${ID}/cancel`],
    ["PUT", `lines/${ID}/money`],
    ["GET", "bookings"],
    ["GET", "settings/payments"],
    ["PUT", "settings/payments"],
    ["GET", "fx/overrides"],
    ["POST", "fx/overrides"],
    ["POST", `trash/booking_line/${ID}/restore`],
  ])("%s %s is forwarded with the agency", (method, path) => {
    expect(matchBff(method, path)?.tenant).toBe(true);
  });

  it.each([
    ["GET", "fx/platform"],
    ["DELETE", "fx/overrides"],
    ["PUT", `lines/${ID}`],
    ["POST", `lines/${ID}/confirm-check`],
    ["GET", `suppliers/${ID}/reveal`],
  ])("%s %s is not", (method, path) => {
    expect(matchBff(method, path)).toBeNull();
  });
});
