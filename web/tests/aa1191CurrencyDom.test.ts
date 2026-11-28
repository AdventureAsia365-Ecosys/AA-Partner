// @vitest-environment jsdom
// AA-1191 review fix, in the browser: a proposal saved in a currency that is no longer enabled (only USD is) is never a
// dead end. "New version" of a sent VND text proposal starts in USD with empty amounts and a note (the POST would be
// refused in VND); editing a VND draft keeps VND (the PATCH sends the stored value back unchanged); a builder whose
// latest version is in VND offers "Start the builder" in USD (its copy is refused, the prices are in VND). Invented
// names only.
import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import type { LifecycleView, Proposal } from "@/lib/proposals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { ProposalPanel } = await import("@/components/proposals/ProposalPanel");

const en = MESSAGES.en;
const TRIP = "00000000-0000-4000-8000-000000001191";
const P = "00000000-0000-4000-8000-000000001192";
const B = "00000000-0000-4000-8000-000000001193";

const VND_TEXT: Proposal = {
  proposal_id: P,
  version: 1,
  status: "sent",
  body: "Five days along the lantern river.",
  currency: "VND",
  total_amount: "45000000.00",
  deposit_amount: "9000000.00",
  balance_due_on: "2026-12-20",
  terms_doc_id: null,
  price_hash: null,
  sent_at: "2026-11-01T00:00:00Z",
  accepted_at: null,
  kind: "text",
};

function lifecycle(proposals: Proposal[]): LifecycleView {
  return { trip_id: TRIP, stage: "proposal", proposals, schedule: [], payments: [], deposit_paid: false, portal_unlocked: false, portal: { s3: 0, waiver_required: false, travellers: [] } };
}

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mount(element: ReactNode): Promise<HTMLElement> {
  const c = document.createElement("div");
  document.body.appendChild(c);
  const root = createRoot(c);
  roots.push(root);
  await act(async () => root.render(element));
  return c;
}

const props = (proposals: Proposal[]) => ({ view: lifecycle(proposals), terms: [], party: [], role: "owner", lang: "en" as const, locale: "en-AU" as const, clientOrigin: null, currencies: ["USD"] });
const button = (c: HTMLElement, label: string) => [...c.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === label);
const field = (c: HTMLElement, name: string) => c.querySelector<HTMLInputElement>(`[data-testid=proposal-form] [name=${name}]`)!;

describe("a proposal in a currency that is no longer enabled", () => {
  it("New version of a sent VND text proposal starts in USD, amounts empty, with a note", async () => {
    const c = await mount(createElement(ProposalPanel, props([VND_TEXT])));
    await act(async () => button(c, en["proposal.newVersion"])!.click());
    expect(field(c, "currency").value).toBe("USD");
    expect(field(c, "total").value).toBe("");
    expect(field(c, "deposit").value).toBe("");
    expect(c.querySelector("[data-testid=proposal-currency-note]")!.textContent).toBe(
      en["proposal.currencySwitched"].replace("{code}", "VND"),
    );
  });

  it("editing a VND draft keeps VND and its amounts (no note)", async () => {
    const c = await mount(createElement(ProposalPanel, props([{ ...VND_TEXT, status: "draft", sent_at: null }])));
    await act(async () => button(c, en["proposal.edit"])!.click());
    expect(field(c, "currency").value).toBe("VND");
    expect(field(c, "total").value).toBe("45000000.00");
    expect(c.querySelector("[data-testid=proposal-currency-note]")).toBeNull();
  });

  it("a builder whose latest version is in VND offers a new start in USD", async () => {
    const built: Proposal = { ...VND_TEXT, proposal_id: B, kind: "builder", status: "sent" };
    const c = await mount(createElement(ProposalPanel, props([built])));
    expect(c.querySelector("[data-testid=builder-currency-note]")!.textContent).toContain("VND");
    expect(button(c, en["builder.start"])).toBeTruthy();
    const usd = await mount(createElement(ProposalPanel, props([{ ...built, currency: "USD" }])));
    expect(button(usd, en["builder.start"])).toBeUndefined(); // an enabled version is copied from the builder page
  });
});
