// @vitest-environment jsdom
// AA-1147: "Deadline and supplier" on an agency line of the Bookings tab. The lines an accepted proposal writes start
// without a deadline, so the agent sets one here (PATCH lines/{id} {due_at}), clears it ({due_at: null}), moves a
// planning line to another own supplier or to "Handled by the agency" (null) and folds a flight or the fee into the
// tour price. Only what changed is sent. Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES } from "@/lib/i18n";
import { canEditService, lineChangeBody, type Line, type Supplier, type TripLines } from "@/lib/ledger";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { LineServiceEdit } = await import("@/components/ledger/LineServiceEdit");
const { LinesPanel } = await import("@/components/ledger/LinesPanel");

const ID = "00000000-0000-4000-8000-0000000000c1";
const SUP = "00000000-0000-4000-8000-0000000000c2";
const SUPPLIERS = [{ agency_supplier_id: SUP, name: "Orchid Lane Air Desk", kind: "own" }] as unknown as Supplier[];

function line(over: Partial<Line> = {}): Line {
  return {
    booking_line_id: ID,
    trip_id: ID,
    supplier_kind: "agency",
    agency_supplier_id: null,
    supplier_name: null,
    category: "flight",
    option_id: "00000000-0000-4000-8000-0000000000c3",
    due_at: null,
    bundled: false,
    status: "planning",
    aa_sub_status: null,
    title: "Return flight to Port Velora",
    confirmation_no: null,
    service_from: "2031-03-01",
    service_to: null,
    collect: "agency",
    confirm_class: null,
    visible_to_travelers: false,
    confirmed_at: null,
    read_only: false,
    aa: null,
    money: null,
    ...over,
  } as Line;
}

function form(fields: Record<string, string>): FormData {
  const f = new FormData();
  for (const [k, v] of Object.entries(fields)) f.set(k, v);
  return f;
}

describe("the line change body", () => {
  it("sends a new deadline and nothing that did not change", () => {
    expect(lineChangeBody(form({ due_on: "2031-02-20", agency_supplier_id: "" }), line())).toEqual({ due_at: "2031-02-20" });
    const dated = line({ due_at: "2031-02-20T00:00:00+00:00" });
    expect(lineChangeBody(form({ due_on: "2031-02-20", agency_supplier_id: "" }), dated)).toEqual({});
    // an empty field keeps the deadline ("Clear deadline" sends null)
    expect(lineChangeBody(form({ due_on: "" }), dated)).toEqual({});
  });

  it("moves the supplier of a planning line only, none = handled by the agency", () => {
    expect(lineChangeBody(form({ agency_supplier_id: SUP }), line())).toEqual({ agency_supplier_id: SUP });
    expect(lineChangeBody(form({ agency_supplier_id: "" }), line({ agency_supplier_id: SUP }))).toEqual({ agency_supplier_id: null });
    expect(lineChangeBody(form({ agency_supplier_id: SUP }), line({ status: "requested" }))).toEqual({});
  });

  it("sends the fold for a flight or the fee only", () => {
    expect(lineChangeBody(form({ bundled: "on" }), line())).toEqual({ bundled: true });
    expect(lineChangeBody(form({}), line({ bundled: true }))).toEqual({ bundled: false });
    expect(lineChangeBody(form({ bundled: "on" }), line({ category: "visa" }))).toEqual({});
  });

  it("applies to a planning or requested agency line of an open trip", () => {
    expect(canEditService(line(), false)).toBe(true);
    expect(canEditService(line({ status: "requested" }), false)).toBe(true);
    expect(canEditService(line({ status: "confirmed" }), false)).toBe(false);
    expect(canEditService(line({ status: "cancelled" }), false)).toBe(false);
    expect(canEditService(line(), true)).toBe(false);
    expect(canEditService(line({ supplier_kind: "aa", read_only: true }), false)).toBe(false);
  });

  it("has its words in both languages", () => {
    for (const key of ["agencyItem.editService", "agencyItem.clearDue"] as const) {
      expect(MESSAGES.en[key], key).toBeTruthy();
      expect(MESSAGES.vi[key], key).toBeTruthy();
      expect(MESSAGES.vi[key], key).not.toBe(MESSAGES.en[key]);
    }
  });
});

describe("the Bookings tab", () => {
  function render(lines: Line[]): string {
    const view: TripLines = { trip_id: ID, lines, base_currency: "USD", booking_validation_level: "basic", lost_reason_hint: null };
    return renderToString(createElement(LinesPanel, { tripId: ID, view, suppliers: SUPPLIERS, role: "agent", lang: "en", locale: "en-AU" }));
  }

  it("offers Deadline and supplier on an agency line from the accepted proposal, not on a confirmed one", () => {
    expect(render([line()])).toContain('data-testid="line-edit-service"');
    expect(render([line({ status: "confirmed", confirmation_no: "PNR7Q" })])).not.toContain('data-testid="line-edit-service"');
  });
});

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mount(l: Line, onSave: (body: Record<string, unknown>) => Promise<boolean>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const tr = (key: string) => MESSAGES.en[key as keyof typeof MESSAGES.en] ?? key;
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(LineServiceEdit, { line: l, suppliers: SUPPLIERS, closed: false, disabled: false, locale: "en-AU", tr, onSave }));
  });
  return container;
}

const q = (c: HTMLElement, id: string) => c.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

async function click(el: HTMLElement | null) {
  await act(async () => el!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

async function typeInto(el: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("Deadline and supplier", () => {
  it("saves the deadline the agent types as the PATCH body", async () => {
    const onSave = vi.fn(async () => true);
    const c = await mount(line(), onSave);
    await click(q(c, "line-edit-service"));
    await typeInto(q(c, "date-input") as HTMLInputElement, "20/02/2031");
    const f = q(c, "line-edit-form") as HTMLFormElement;
    await act(async () => f.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(onSave).toHaveBeenCalledWith({ due_at: "2031-02-20" });
    expect(q(c, "line-edit-form")).toBeNull();
  });

  it("clears a deadline with due_at null", async () => {
    const onSave = vi.fn(async () => true);
    const c = await mount(line({ due_at: "2031-02-20T00:00:00+00:00", status: "requested" }), onSave);
    await click(q(c, "line-edit-service"));
    expect(q(c, "line-edit-supplier")).toBeNull(); // a requested line keeps its supplier
    await click(q(c, "line-edit-clear-due"));
    expect(onSave).toHaveBeenCalledWith({ due_at: null });
  });
});
