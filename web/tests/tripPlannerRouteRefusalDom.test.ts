// @vitest-environment jsdom
// AA-1139 review fix, in the browser: the "Where leads go" section never swallows a refusal. CIS checks the stored
// target of the channel not chosen too (it goes live when the channel is switched), so a 422 can be about a field that
// is not on screen: the section then shows the reason as its message, naming the hidden field. A refusal of the field
// on screen stays under that field. Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fmt, MESSAGES } from "@/lib/i18n";
import type { BffResult } from "@/lib/bffClient";
import {
  hiddenRefusalKey,
  inlineRefusalField,
  type PlannerPage,
  type TripPlannerModule,
} from "@/lib/tripPlannerModule";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { TripPlannerModuleView } = await import("@/components/settings/TripPlannerModuleView");

const en = MESSAGES.en;
const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

function page(route: Partial<TripPlannerModule["lead_route"]>): PlannerPage {
  return {
    module: {
      active: true,
      in_service: true,
      planner: { url: "https://planner.example.invalid/quillmoor-tours/plan", domains: [], launcher: true },
      domains: [],
      crm_core: false,
      lead_route: { lead_channel: "webhook", lead_webhook_url: "https://hooks.quillmoor.example.com/leads",
                    lead_email: "desk@quillmoor.internal", effective_channel: "webhook", updated_at: null, ...route },
      theme: {},
      locale: "en",
      ai_sessions_monthly: 300,
    },
    usage: null,
    webhook_key: null,
  };
}

async function mount(p: PlannerPage, detail: string): Promise<{ el: HTMLElement; sent: unknown[] }> {
  const sent: unknown[] = [];
  const send = async <T,>(_m: "PUT" | "POST", path: string, body?: unknown): Promise<BffResult<T>> => {
    sent.push({ path, body });
    return { ok: false, status: 422, detail };
  };
  const el = document.createElement("div");
  document.body.appendChild(el);
  await act(async () => {
    const root = createRoot(el);
    roots.push(root);
    root.render(createElement(TripPlannerModuleView, { page: p, leads: null, owner: true, lang: "en", locale: "en-GB",
                                                       send: send as never, onRefresh: () => {} }));
  });
  return { el, sent };
}

async function save(el: HTMLElement) {
  const button = el.querySelector<HTMLButtonElement>('[data-testid="tp-route-save"] button')!;
  await act(async () => { button.click(); });
}

describe("lead route refusals", () => {
  it("places a refusal under the field on screen, else as the section's message", () => {
    expect(inlineRefusalField("webhook", "lead_webhook_url")).toBe("lead_webhook_url");
    expect(inlineRefusalField("email", "lead_email")).toBe("lead_email");
    expect(inlineRefusalField("webhook", "lead_email")).toBeNull();
    expect(inlineRefusalField("email", "lead_webhook_url")).toBeNull();
    expect(inlineRefusalField("none", "lead_email")).toBeNull();
    expect(inlineRefusalField("email", null)).toBeNull();
    for (const lang of ["en", "vi"] as const) {
      for (const field of ["lead_email", "lead_webhook_url"] as const) {
        expect(MESSAGES[lang][hiddenRefusalKey(field)]).toContain("{reason}");
      }
    }
  });

  it("names a refused e-mail address that is not on screen (webhook chosen)", async () => {
    const { el, sent } = await mount(page({}), "lead_email_not_public");
    await save(el);
    expect(sent).toHaveLength(1);
    const message = el.querySelector('[data-testid="tp-route-message"]');
    expect(message?.textContent).toBe(fmt(en["tp.route.hiddenEmail"], { reason: en["tp.err.lead_email_not_public"] }));
    expect(message?.getAttribute("role")).toBe("alert");
  });

  it("names a refused webhook URL when leads go nowhere (no field on screen)", async () => {
    const { el } = await mount(page({ lead_channel: "none", effective_channel: "none" }), "lead_webhook_url_not_public");
    await save(el);
    expect(el.querySelector('[data-testid="tp-route-message"]')?.textContent)
      .toBe(fmt(en["tp.route.hiddenWebhook"], { reason: en["tp.err.lead_webhook_url_not_public"] }));
  });

  it("keeps a refusal of the field on screen under that field", async () => {
    const { el } = await mount(page({}), "lead_webhook_url_not_public");
    await save(el);
    expect(el.querySelector('[data-testid="tp-route-message"]')).toBeNull();
    expect(el.querySelector('[data-testid="tp-route"]')?.textContent).toContain(en["tp.err.lead_webhook_url_not_public"]);
  });

  it("shows a refusal about the tenant as the section's message as before", async () => {
    const { el } = await mount(page({}), "tripplanner_not_active");
    await save(el);
    expect(el.querySelector('[data-testid="tp-route-message"]')?.textContent).toBe(en["tp.err.notActive"]);
  });
});
