// AA-936 — owner rule S249: every page opens in English; Vietnamese (or another date locale) only when the person
// picks it (cookie aa_crm_locale). Neither the agency's default_locale nor the browser's Accept-Language switch the UI.
// The public inquiry form is English too unless the form itself sets `locale`.
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearFormMemo, type PublicForm } from "@/lib/intakeForms";
import { testConfig } from "./helpers";

const req = { cookie: undefined as string | undefined, acceptLanguage: "vi-VN,vi;q=0.9" };

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (name === "aa_crm_locale" && req.cookie ? { name, value: req.cookie } : undefined),
  }),
  headers: async () => new Headers({ "accept-language": req.acceptLanguage }),
}));

const { serverContext } = await import("@/lib/serverContext");
const { default: InquiryPage } = await import("@/app/c/f/[publicKey]/page");

const KEY = "AbCdEfGhIjKlMnOpQrStUv";
const FORM: PublicForm = {
  schema_key: "stage1.v1",
  version: 1,
  fields: [{ name: "destinations", type: "multi_select", required: true, options: ["vietnam"] }],
  honeypot_field: "website",
  agency_name: "Lữ hành Sen Vàng",
  default_locale: "vi-VN",
  allowed_origins: [],
  privacy_notice_url: null,
};

beforeEach(() => {
  testConfig();
  clearFormMemo();
  req.cookie = undefined;
  req.acceptLanguage = "vi-VN,vi;q=0.9";
});
afterEach(() => vi.unstubAllGlobals());

describe("server context language", () => {
  it("is English (en-AU) for a Vietnamese browser and a vi-VN agency when the person picked nothing", async () => {
    const ctx = await serverContext();
    expect(ctx.locale).toBe("en-AU");
    expect(ctx.lang).toBe("en");
  });

  it("follows the person's saved choice", async () => {
    req.cookie = "vi-VN";
    expect((await serverContext()).lang).toBe("vi");
    req.cookie = "en-US";
    expect((await serverContext()).locale).toBe("en-US");
  });
});

describe("inquiry form page (client host)", () => {
  async function render(form: PublicForm): Promise<string> {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(form), { status: 200 })));
    return renderToString(await InquiryPage({ params: Promise.resolve({ publicKey: KEY }) }));
  }

  it("is English for a vi-VN agency whose form sets no language", async () => {
    const html = await render(FORM);
    expect(html).toContain('lang="en"');
    expect(html).toContain("Plan your trip with Lữ hành Sen Vàng");
    expect(html).not.toContain("Lên kế hoạch");
  });

  it("is Vietnamese when the form itself sets locale vi", async () => {
    clearFormMemo();
    const html = await render({ ...FORM, locale: "vi" });
    expect(html).toContain('lang="vi"');
    expect(html).toContain("Lên kế hoạch chuyến đi cùng Lữ hành Sen Vàng");
  });

  it("renders the submit disabled and the form unmarked until hydration", async () => {
    const html = await render(FORM);
    expect(html).toMatch(/data-testid="intake-submit"><button[^>]*disabled/);
    expect(html).not.toContain('data-hydrated="true"');
  });
});
