// @vitest-environment jsdom
// AA-1036 (R3, root ADR 0019 d7) — Account › Add-ons (AccountAddOns): the owner asks for an add-on (POST to the CIS
// request route through the studio BFF), an editor reads why it cannot, a pending request shows "Requested", a decline
// shows its reason, an active CRM links the CRM home. Fake agency names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountAddOns, refusalText } from "@/app/(studio)/portal/_components/AccountAddOns";
import { ADDONS_MESSAGES } from "@/app/(studio)/portal/_components/account.messages";
import { PortalShellContext, type PortalShellValue } from "@/app/(studio)/portal/_components/PortalShellContext";
import { ApiError } from "@/app/_kit";
import { translator } from "@/app/_kit/core/i18n";
import { CRM_HOME_PATH } from "@/lib/nav";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

type AddOn = { product: string; state: string; subscription_status: string | null; request: unknown; last_decision: unknown };
const req = (over: Record<string, unknown> = {}) => ({
  request_id: "r1", product: "crm", status: "pending", note: null, requested_at: "2026-11-04T08:00:00Z",
  decided_at: null, decision_note: null, ...over,
});
const available = (product: string): AddOn => ({ product, state: "available", subscription_status: null, request: null, last_decision: null });

function stubApi(addOns: AddOn[], role: string, post?: (body: unknown) => Response, crmMember = false) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, method, body });
    if (url === "/api/tenant/me") return Response.json({ tenant_name: "Lantern Bay Travel", content_studio_role: role });
    if (url === "/api/tenant/v1/account/add-ons") return Response.json({ in_service: true, add_ons: addOns });
    if (url === "/api/studio/apps") return Response.json({ apps: [], crm_member: crmMember });
    if (url === "/api/tenant/v1/account/add-on-requests" && post) return post(body);
    return Response.json({}, { status: 404 });
  }));
  return calls;
}

async function mount() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const toasts: string[] = [];
  const shell = { showToast: (m: string) => toasts.push(m) } as unknown as PortalShellValue;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(QueryClientProvider, { client: qc },
      createElement(PortalShellContext.Provider, { value: shell }, createElement(AccountAddOns))));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  return { q: (s: string) => container.querySelector(s), text: () => container.textContent ?? "", toasts };
}

describe("Account › Add-ons", () => {
  it("the owner asks for the CRM with a note; the request goes to the CIS route through the BFF", async () => {
    const calls = stubApi([available("crm"), available("tripplanner")], "owner",
      () => Response.json({ created: true, request: req() }, { status: 201 }));
    const { q, text, toasts } = await mount();
    expect(q("[data-testid=add-on-state-crm]")?.getAttribute("data-state")).toBe("available");
    const textarea = q("[data-testid=add-on-crm] textarea") as HTMLTextAreaElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      set?.call(textarea, "Five agents would use it");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const button = q("[data-testid=add-on-request-crm] button") as HTMLButtonElement;
    expect(button.textContent).toBe("Request CRM");
    await act(async () => {
      button.click();
      await new Promise((r) => setTimeout(r, 20));
    });
    const post = calls.find((c) => c.method === "POST");
    expect(post).toEqual({ url: "/api/tenant/v1/account/add-on-requests", method: "POST",
                           body: { product: "crm", note: "Five agents would use it" } });
    expect(toasts).toEqual(["Request for CRM sent. Adventure Asia will reply here."]);
    expect(text()).not.toContain("mailto:");
  });

  it("CIS's 409 tenant_not_in_service reads as its own sentence, not 'already on' (review AA-1036)", async () => {
    stubApi([available("crm"), available("tripplanner")], "owner",
      () => Response.json({ detail: { detail: "tenant_not_in_service", message: "this account cannot request add-ons now" } },
                          { status: 409 }));
    const { q } = await mount();
    await act(async () => {
      (q("[data-testid=add-on-request-crm] button") as HTMLButtonElement).click();
      await new Promise((r) => setTimeout(r, 20));
    });
    const t = translator(ADDONS_MESSAGES, "en");
    expect(q("[data-testid=add-on-crm] [role=alert]")?.textContent).toBe(t("errNotInService"));
  });

  it("an editor sees who may ask instead of the form", async () => {
    stubApi([available("crm"), available("tripplanner")], "editor");
    const { q, text } = await mount();
    expect(q("[data-testid=add-on-request-crm]")).toBeNull();
    expect(text()).toContain("Only the workspace owner can request an add-on.");
  });

  it("shows a pending request, a decline with its reason, and an active CRM with its link", async () => {
    stubApi([
      { product: "crm", state: "active", subscription_status: "active", request: null,
        last_decision: req({ status: "approved", decided_at: "2026-11-05T09:00:00Z", setup: "done" }) },
      { product: "tripplanner", state: "requested", subscription_status: null,
        request: req({ product: "tripplanner", note: "For our new website" }), last_decision: null },
    ], "owner", undefined, true);
    const { q, text } = await mount();
    expect(q("[data-testid=add-on-state-crm]")?.getAttribute("data-state")).toBe("active");
    expect(q(`[data-testid=add-on-crm] a[href="${CRM_HOME_PATH}"]`)).not.toBeNull();
    expect(q("[data-testid=add-on-ask-crm-access]")).toBeNull();
    expect(q("[data-testid=add-on-state-tripplanner]")?.textContent).toContain("Requested");
    expect(text()).toContain("For our new website");
    expect(q("[data-testid=add-on-request-tripplanner]")).toBeNull();
  });

  it("a declined request shows the reason and the owner may ask again", async () => {
    stubApi([{ ...available("crm"), last_decision: req({ status: "declined", decided_at: "2026-11-05T09:00:00Z",
                                                          decision_note: "The CRM opens after the pilot." }) },
             available("tripplanner")], "owner");
    const { q, text } = await mount();
    expect(text()).toContain("The CRM opens after the pilot.");
    expect(q("[data-testid=add-on-request-crm] button")).not.toBeNull();
  });

  it("an active CRM offers no CRM link to a person without a CRM role (review AA-1036)", async () => {
    stubApi([{ product: "crm", state: "active", subscription_status: "active", request: null, last_decision: null },
             available("tripplanner")], "editor", undefined, false);
    const { q, text } = await mount();
    expect(q(`[data-testid=add-on-crm] a[href="${CRM_HOME_PATH}"]`)).toBeNull();
    expect(q("[data-testid=add-on-ask-crm-access]")).not.toBeNull();
    expect(text()).toContain("Ask your owner for CRM access.");
  });

  it("an approval whose setup failed or still runs says so, never a bare 'Approved' (review AA-1036)", async () => {
    stubApi([{ ...available("crm"), last_decision: req({ status: "approved", decided_at: "2026-11-05T09:00:00Z",
                                                          setup: "failed" }) },
             { ...available("tripplanner"), last_decision: req({ product: "tripplanner", status: "approved",
                                                                 decided_at: "2026-11-05T09:00:00Z", setup: "in_progress" }) }],
            "owner");
    const { q } = await mount();
    expect(q("[data-testid=add-on-decision-crm]")?.textContent).toContain("the setup did not finish");
    expect(q("[data-testid=add-on-decision-tripplanner]")?.textContent).toContain("is switching it on");
  });

  it("maps the refusals to plain sentences", () => {
    const t = translator(ADDONS_MESSAGES, "en");
    expect(refusalText(t, new ApiError("x", 403))).toBe(t("errOwnerOnly"));
    expect(refusalText(t, new ApiError("x", 409, "", "already_subscribed"))).toBe(t("errAlreadyOn"));
    // review AA-1036: 409 has two causes; an account out of service is not "already on"
    expect(refusalText(t, new ApiError("x", 409, "", "tenant_not_in_service"))).toBe(t("errNotInService"));
    expect(refusalText(t, new ApiError("x", 409))).toBe(t("errSend"));
    expect(translator(ADDONS_MESSAGES, "vi")("errNotInService")).not.toBe(t("errNotInService"));
    expect(refusalText(t, new ApiError("x", 429))).toBe(t("errTooMany"));
    expect(refusalText(t, new Error("down"))).toBe(t("errSend"));
  });
});
