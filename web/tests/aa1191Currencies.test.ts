// tests/aa1191Currencies.test.ts — AA-1191: USD only unless the platform enables another currency (CIS setting,
// GET /crm/v1/currencies); pickers list the enabled currencies and hide while there is no choice.
import { describe, expect, it } from "vitest";

import { BASE_CURRENCY, enabledCurrencies, hasCurrencyChoice } from "../lib/currencies";

describe("enabled currencies", () => {
  it("keeps USD first and defaults to USD only", () => {
    expect(enabledCurrencies(undefined)).toEqual(["USD"]);
    expect(enabledCurrencies(null)).toEqual(["USD"]);
    expect(enabledCurrencies(["VND"])).toEqual(["USD", "VND"]);
    expect(enabledCurrencies([" vnd ", "USD", "VND"])).toEqual(["USD", "VND"]);
    expect(BASE_CURRENCY).toBe("USD");
  });

  it("shows a picker only when there is a choice", () => {
    expect(hasCurrencyChoice(["USD"])).toBe(false);
    expect(hasCurrencyChoice(undefined)).toBe(false);
    expect(hasCurrencyChoice(["USD", "VND"])).toBe(true);
  });
});
