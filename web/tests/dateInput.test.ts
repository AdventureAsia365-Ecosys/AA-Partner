// @vitest-environment jsdom
// AA-935 S249 sign-in check: on /settings "03/04/2026" was typed into the date field and the reading next to it stayed
// empty. The page is server-rendered; the field was filled before React hydrated it. React keeps the typed text in the
// DOM when it hydrates a controlled input but never fires onChange for it, so the component state stayed "" (no
// reading, empty hidden ISO value) until the next keystroke. These tests reproduce that with renderToString +
// hydrateRoot and check the reading for the agency locale (vi-VN) and en-AU.
import { act, createElement } from "react";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";
import { DateInput } from "@/components/DateInput";
import type { DateLocale } from "@/lib/dates";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

function field(locale: DateLocale) {
  return createElement(DateInput, {
    locale,
    name: "sample_date",
    label: "Try a date",
    readAs: locale === "vi-VN" ? "Hiểu là" : "Read as",
    invalid: "Not a date",
  });
}

/** Server HTML in the document, as the browser has it before the page's JavaScript runs. */
function serverRendered(locale: DateLocale) {
  const container = document.createElement("div");
  container.innerHTML = renderToString(field(locale));
  document.body.appendChild(container);
  const input = container.querySelector<HTMLInputElement>("[data-testid=date-input]")!;
  const readAs = () => container.querySelector("[data-testid=date-read-as]")!.textContent;
  const iso = () => container.querySelector<HTMLInputElement>("input[type=hidden][name=sample_date]")!.value;
  return { container, input, readAs, iso };
}

async function hydrate(container: HTMLElement, locale: DateLocale) {
  await act(async () => {
    roots.push(hydrateRoot(container, field(locale)));
  });
}

/** What a browser does on a keystroke after hydration: the native value setter, then an `input` event. */
async function type(input: HTMLInputElement, text: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("DateInput reads what was typed before hydration", () => {
  it("vi-VN (agency Lữ hành Sen Vàng): 03/04/2026 typed early reads 3 thg 4, 2026", async () => {
    const page = serverRendered("vi-VN");
    expect(page.input.placeholder).toBe("DD/MM/YYYY");
    page.input.value = "03/04/2026"; // typed while the scripts were still loading
    await hydrate(page.container, "vi-VN");
    expect(page.input.value).toBe("03/04/2026");
    expect(page.readAs()).toMatch(/^Hiểu là: 3 thg 4,? 2026$/);
    expect(page.iso()).toBe("2026-04-03");
  });

  it("en-AU: 03/04/2026 typed early reads 3 Apr 2026", async () => {
    const page = serverRendered("en-AU");
    page.input.value = "03/04/2026";
    await hydrate(page.container, "en-AU");
    expect(page.readAs()).toBe("Read as: 3 Apr 2026");
    expect(page.iso()).toBe("2026-04-03");
  });

  it("an untouched field stays empty after hydration", async () => {
    const page = serverRendered("vi-VN");
    await hydrate(page.container, "vi-VN");
    expect(page.readAs()).toBe("");
    expect(page.iso()).toBe("");
    expect(page.container.querySelector("[data-testid=date-input]")!.getAttribute("data-hydrated")).toBe("true");
  });
});

describe("DateInput after hydration", () => {
  it("reads typing in the locale order and flags what is not a date", async () => {
    const page = serverRendered("vi-VN");
    await hydrate(page.container, "vi-VN");
    await type(page.input, "03/04/2026");
    expect(page.readAs()).toMatch(/^Hiểu là: 3 thg 4,? 2026$/);
    expect(page.iso()).toBe("2026-04-03");
    await type(page.input, "31/02/2026");
    expect(page.readAs()).toBe("Not a date");
    expect(page.input.getAttribute("aria-invalid")).toBe("true");
    expect(page.iso()).toBe("");
  });

  it("en-US keeps month-first", async () => {
    const page = serverRendered("en-US");
    await hydrate(page.container, "en-US");
    await type(page.input, "03/04/2026");
    expect(page.readAs()).toBe("Read as: Mar 4, 2026");
  });
});
