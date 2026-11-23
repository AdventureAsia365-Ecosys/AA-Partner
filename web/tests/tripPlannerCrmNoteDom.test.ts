// @vitest-environment jsdom
// AA-1160, in the browser: "Where leads go" says the CRM only when leads really go there. With the CRM add-on but no
// CRM agency yet (S285: crm.core, no agency, leads skipped), CIS answers effective_channel = the stored route, and the
// section says the CRM is not taking planner leads instead of "Leads go to your CRM". Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import type { PlannerPage, TripPlannerModule } from "@/lib/tripPlannerModule";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { TripPlannerModuleView } = await import("@/components/settings/TripPlannerModuleView");

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

function page(crmCore: boolean, effective: TripPlannerModule["lead_route"]["effective_channel"]): PlannerPage {
  return {
    module: {
      active: true,
      in_service: true,
      planner: { url: "https://planner.example.invalid/brackenford-trails/plan", domains: [], launcher: true },
      domains: [],
      crm_core: crmCore,
      lead_route: { lead_channel: "email", lead_webhook_url: null, lead_email: "desk@brackenford.example.com",
                    effective_channel: effective, updated_at: null },
      theme: {},
      locale: "vi",
      ai_sessions_monthly: 300,
    },
    usage: null,
    webhook_key: null,
  };
}

async function mount(p: PlannerPage, lang: "en" | "vi"): Promise<HTMLElement> {
  const el = document.createElement("div");
  document.body.appendChild(el);
  await act(async () => {
    const root = createRoot(el);
    roots.push(root);
    root.render(createElement(TripPlannerModuleView, { page: p, leads: null, owner: true, lang, locale: "en-GB",
                                                       send: (async () => ({ ok: true, status: 200 })) as never,
                                                       onRefresh: () => {} }));
  });
  return el;
}

describe("the CRM note of Where leads go", () => {
  it("says the CRM only when leads go to the CRM", async () => {
    const el = await mount(page(true, "crm"), "en");
    expect(el.textContent).toContain(MESSAGES.en["tp.route.crm"]);
    expect(el.querySelector('[data-testid="tp-route-no-agency"]')).toBeNull();
  });

  it("with the add-on but no CRM agency yet, says the route below applies", async () => {
    for (const lang of ["en", "vi"] as const) {
      const el = await mount(page(true, "email"), lang);
      expect(el.querySelector('[data-testid="tp-route-no-agency"]')?.textContent).toBe(MESSAGES[lang]["tp.route.noAgency"]);
      expect(el.textContent).not.toContain(MESSAGES[lang]["tp.route.crm"]);
      for (const r of roots.splice(0)) act(() => r.unmount());
    }
  });

  it("without the add-on, no CRM note at all", async () => {
    const el = await mount(page(false, "email"), "en");
    expect(el.querySelector('[data-testid="tp-route-no-agency"]')).toBeNull();
    expect(el.textContent).not.toContain(MESSAGES.en["tp.route.crm"]);
  });
});
