// @vitest-environment jsdom
// S264 — the CRM sidebar groups fold (kit core NavGroup + useNavGroups, scope "crm-web"). On /payments the Money group
// (current page) is open and CRM / Grow are folded; Home and Settings have no header and stay open; unfolding a group
// stores the choice in localStorage `aa_nav_groups:crm-web`; folding the active group hides its links on this page.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/payments",
  useRouter: () => ({ push: () => {}, replace: () => {}, refresh: () => {}, prefetch: () => {} }),
}));
vi.mock("@/components/BellMenu", () => ({ BellMenu: () => null }));
vi.mock("@/components/LocaleSwitch", () => ({ LocaleSwitch: () => null }));

import { CrmShell, type ShellProps } from "@/app/_kit/local/CrmShell";
import { dictionary } from "@/lib/i18n";

// The shell with `children` passed as the third createElement argument (react/no-children-prop).
const Shell = CrmShell as unknown as React.FC<Omit<ShellProps, "children"> & { children?: React.ReactNode }>;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no matchMedia (the theme button reads prefers-color-scheme).
window.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;

const roots: Root[] = [];
beforeEach(() => window.localStorage.clear());
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mount(contentStudioHref: string | null = "/studio") {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(
      createElement(
        Shell,
        {
          dict: dictionary("en"),
          role: "owner",
          agencyName: "Lantern Bay Travel",
          agencyCount: 1,
          contentStudioHref,
          plannerRequestHref: "mailto:hello@example.test?subject=Add-on",
          locale: "en-AU",
          syntheticBadge: false,
        },
        createElement("p", null, "page"),
      ),
    );
  });
  const q = (sel: string) => container.querySelector<HTMLElement>(sel);
  return { container, q };
}

const toggle = (q: (s: string) => HTMLElement | null, group: string) => q(`[data-testid=crm-nav-group-${group}-toggle]`);
const region = (q: (s: string) => HTMLElement | null, group: string) =>
  q(`[data-testid=crm-nav-group-${group}] [role=group]`);

describe("CRM sidebar groups", () => {
  it("opens the current page group and folds the others", async () => {
    const { q } = await mount();
    expect(toggle(q, "money")?.getAttribute("aria-expanded")).toBe("true");
    expect(region(q, "money")?.hidden).toBe(false);
    for (const g of ["crm", "grow", "studio"]) {
      expect(toggle(q, g)?.getAttribute("aria-expanded")).toBe("false");
      expect(region(q, g)?.hidden).toBe(true);
    }
    // Home and Settings: no header, always open.
    expect(toggle(q, "home")).toBeNull();
    expect(toggle(q, "settings")).toBeNull();
    expect(region(q, "settings")?.hidden).toBe(false);
    // aria-controls points at the items element.
    const id = toggle(q, "crm")?.getAttribute("aria-controls");
    expect(id && document.getElementById(id)).toBe(region(q, "crm"));
  });

  it("unfolds a group on click and stores the choice", async () => {
    const { q } = await mount();
    await act(async () => toggle(q, "grow")!.click());
    expect(toggle(q, "grow")?.getAttribute("aria-expanded")).toBe("true");
    expect(region(q, "grow")?.hidden).toBe(false);
    expect(window.localStorage.getItem("aa_nav_groups:crm-web")).toBe("grow=open");
  });

  it("restores a stored open group and lets the user fold the active one", async () => {
    window.localStorage.setItem("aa_nav_groups:crm-web", "crm=open");
    const { q } = await mount();
    expect(toggle(q, "crm")?.getAttribute("aria-expanded")).toBe("true");
    await act(async () => toggle(q, "money")!.click());
    expect(toggle(q, "money")?.getAttribute("aria-expanded")).toBe("false");
    expect(region(q, "money")?.hidden).toBe(true);
    expect(toggle(q, "money")?.getAttribute("data-active")).toBe("true");
    expect(window.localStorage.getItem("aa_nav_groups:crm-web")).toBe("crm=open,money=closed");
  });
});

// AA-1036 (root ADR 0019 d7): the Content studio group is the app switcher.
describe("CRM app switcher", () => {
  it("lists the Content studio and the Trip planner as an add-on to request", async () => {
    const { q } = await mount();
    expect(q("[data-testid=crm-studio-link]")?.getAttribute("href")).toBe("/studio");
    const planner = q("[data-testid=crm-app-planner]");
    expect(planner?.getAttribute("href")).toBe("mailto:hello@example.test?subject=Add-on");
    expect(planner?.getAttribute("data-app-state")).toBe("request");
    expect(planner?.textContent).toContain("Trip planner: request add-on");
    expect(q("[data-testid=crm-nav-group-studio-toggle]")?.textContent).toContain("Apps");
  });

  it("keeps the group when the person has no Content studio (the planner request stays)", async () => {
    const { q } = await mount(null);
    expect(q("[data-testid=crm-studio-link]")).toBeNull();
    expect(q("[data-testid=crm-app-planner]")).not.toBeNull();
  });
});
