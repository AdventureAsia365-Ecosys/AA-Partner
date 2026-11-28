// tests/aa1191Currencies.test.ts — AA-1191: USD only unless the platform enables another currency (CIS setting,
// GET /crm/v1/currencies); pickers list the enabled currencies and hide while there is no choice.
import { describe, expect, it } from "vitest";

import { BASE_CURRENCY, builderStart, canCopyBuilderVersion, enabledCurrencies, hasCurrencyChoice, proposalFormCurrency, proposalFormSeed } from "../lib/currencies";

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

describe("the text proposal form currency", () => {
  it("keeps an enabled saved currency, with a picker only when there is a choice", () => {
    expect(proposalFormCurrency("USD", ["USD"], false)).toEqual({ currency: "USD", switchedFrom: null, choice: false });
    expect(proposalFormCurrency("VND", ["USD", "VND"], false)).toEqual({ currency: "VND", switchedFrom: null, choice: true });
    expect(proposalFormCurrency(null, null, false)).toEqual({ currency: "USD", switchedFrom: null, choice: false });
  });

  it("keeps a saved draft's currency that is no longer enabled (the PATCH sends it back unchanged)", () => {
    expect(proposalFormCurrency("VND", ["USD"], true)).toEqual({ currency: "VND", switchedFrom: null, choice: false });
  });

  it("starts a new version of a VND proposal in USD and names the old code", () => {
    expect(proposalFormCurrency("VND", ["USD"], false)).toEqual({ currency: "USD", switchedFrom: "VND", choice: false });
    expect(proposalFormCurrency("VND", ["USD", "EUR"], false)).toEqual({ currency: "USD", switchedFrom: "VND", choice: true });
  });

  it("copies a builder version only while its currency is enabled", () => {
    expect(canCopyBuilderVersion("USD", ["USD"])).toBe(true);
    expect(canCopyBuilderVersion("VND", ["USD"])).toBe(false);
    expect(canCopyBuilderVersion("VND", ["USD", "VND"])).toBe(true);
  });
});

describe("the form seed and the builder start row", () => {
  const sent = { currency: "VND", status: "sent", total_amount: "45000000.00", deposit_amount: "9000000.00" };

  it("empties the amounts only when a new version switches to USD", () => {
    expect(proposalFormSeed(sent, ["USD"])).toMatchObject({ currency: "USD", switchedFrom: "VND", total: "", deposit: "" });
    expect(proposalFormSeed({ ...sent, status: "draft" }, ["USD"])).toMatchObject({ currency: "VND", total: "45000000.00" });
    expect(proposalFormSeed(sent, ["USD", "VND"])).toMatchObject({ currency: "VND", total: "45000000.00", deposit: "9000000.00" });
    expect(proposalFormSeed(null, null)).toEqual({ currency: "USD", switchedFrom: null, choice: false, total: "", deposit: "" });
  });

  it("opens the start row for the first version or a latest version that cannot be copied", () => {
    expect(builderStart([], ["USD"])).toEqual({ open: true, blocked: null });
    expect(builderStart([{ version: 1, currency: "USD" }], ["USD"])).toEqual({ open: false, blocked: null });
    expect(builderStart([{ version: 1, currency: "USD" }, { version: 2, currency: "VND" }], ["USD"])).toEqual({ open: true, blocked: "VND" });
    expect(builderStart([{ version: 2, currency: "USD" }, { version: 1, currency: "VND" }], ["USD"])).toEqual({ open: false, blocked: null });
  });
});
