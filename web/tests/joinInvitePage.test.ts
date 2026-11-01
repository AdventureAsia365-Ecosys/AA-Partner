// S263 join fix (AA-939, found live on Dev): the invitation page shows the one-time password of THIS invitation only,
// sends "Open invitation" once, tells a person who never saw a password what to do, and the language picker labels
// its sample date as an example.
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { localeOptionLabel, LocaleSwitch } from "@/components/LocaleSwitch";
import { MESSAGES } from "@/lib/i18n";
import { claimCookie, inviteCookie, inviteCookieNames, type ClaimResult } from "@/lib/invite";
import { testConfig } from "./helpers";

const jar = new Map<string, string>();

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (jar.has(name) ? { name, value: jar.get(name)! } : undefined),
  }),
  headers: async () => new Headers({ host: "partner.example.test" }),
}));

const { default: JoinInvitePage } = await import("@/app/join/invite/page");

const TOKEN = "C".repeat(20) + "f_g-" + "7".repeat(19);
const OTHER = "D".repeat(20) + "h_i-" + "6".repeat(19);
let cfg = testConfig();

beforeEach(() => {
  cfg = testConfig();
  jar.clear();
});

function invited(token = TOKEN) {
  jar.set(inviteCookieNames(cfg).token, inviteCookie(cfg, token).value);
}

function claimed(claim: ClaimResult, token = TOKEN) {
  const c = claimCookie(cfg, claim, token);
  jar.set(c.name, c.value);
}

async function page(error?: string): Promise<string> {
  return renderToStaticMarkup(await JoinInvitePage({ searchParams: Promise.resolve(error ? { error } : {}) }));
}

describe("/join/invite", () => {
  it("before the claim: one kit submit button that posts the claim once", async () => {
    invited();
    const html = await page();
    expect(html).toContain('action="/join/claim"');
    expect(html).toContain('data-testid="join-open"');
    expect(html).toContain('type="submit"');
    expect(html).toContain("aa-kit-btn--primary");
    expect(html).toContain(MESSAGES.en["join.open"]);
  });

  it("shows the one-time password of this invitation", async () => {
    invited();
    const pw = ["Pw", "shown"].join("-");
    claimed({ state: "created", email: "kim@agency.example", temporaryPassword: pw });
    const html = await page();
    expect(html).toContain('data-testid="join-temporary-password"');
    expect(html).toContain(pw);
    expect(html).not.toContain('action="/join/claim"');
  });

  it("never shows the answer of another invitation (a stale claim cookie of an earlier link)", async () => {
    invited(TOKEN);
    claimed({ state: "created", email: "kim@agency.example", temporaryPassword: "x" }, OTHER);
    claimed({ state: "already_claimed", email: "kim@agency.example", temporaryPassword: null }, OTHER);
    const html = await page();
    expect(html).not.toContain('data-testid="join-claimed"');
    expect(html).not.toContain("kim@agency.example");
    expect(html).toContain('action="/join/claim"');
  });

  it("shows the password when a later already_claimed answer sits next to it (double submit)", async () => {
    invited();
    const pw = ["Pw", "first"].join("-");
    claimed({ state: "created", email: "kim@agency.example", temporaryPassword: pw });
    claimed({ state: "already_claimed", email: "kim@agency.example", temporaryPassword: null });
    const html = await page();
    expect(html).toContain(pw);
    expect(html).not.toContain(MESSAGES.en["join.alreadyClaimedHelp"]);
  });

  it("already claimed: says to ask for a new invitation when no password was ever seen (EN and VI)", async () => {
    invited();
    claimed({ state: "already_claimed", email: "kim@agency.example", temporaryPassword: null });
    const html = await page();
    expect(html).toContain("If you never saw a one-time password, ask the agency owner or Adventure Asia for a new invitation.");
    expect(MESSAGES.vi["join.alreadyClaimedHelp"]).toContain("Adventure Asia");
    expect(MESSAGES.vi["join.opening"]).toBeTruthy();
  });

  it("labels the sample date of the language picker as an example", async () => {
    invited();
    const html = await page();
    expect(html).toContain("English (US) · dates like Apr 3, 2026");
  });
});

describe("LocaleSwitch option labels", () => {
  it("say the date is an example, in the page's language; compact options stay short", () => {
    expect(localeOptionLabel("en-US", MESSAGES.en["shell.dateExample"])).toBe("English (US) · dates like Apr 3, 2026");
    expect(localeOptionLabel("en-GB", MESSAGES.vi["shell.dateExample"])).toMatch(/^English \(UK\) · ngày dạng 3 Apr 2026$/);
    expect(localeOptionLabel("vi-VN", "x", true)).toBe("Tiếng Việt");
    const html = renderToStaticMarkup(createElement(LocaleSwitch, { locale: "en-AU", label: "L", next: "/x", example: "e.g." }));
    expect(html).toContain("English (Australia) · e.g. ");
  });
});
