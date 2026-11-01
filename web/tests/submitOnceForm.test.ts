// @vitest-environment jsdom
// S263 join fix (AA-939): "Open invitation" sends its form once. The first submit goes through and disables the
// button; a second submit (double click, Enter twice) is dropped; a page restored from the back-forward cache is
// ready again.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { SubmitOnceForm } from "@/components/SubmitOnceForm";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mount() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(SubmitOnceForm, { action: "/join/claim", label: "Open invitation", pendingLabel: "Opening…", testId: "join-open" }));
  });
  const form = container.querySelector("form")!;
  const button = container.querySelector<HTMLButtonElement>("[data-testid=join-open]")!;
  return { form, button };
}

function submit(form: HTMLFormElement): boolean {
  const e = new Event("submit", { bubbles: true, cancelable: true });
  form.dispatchEvent(e);
  return e.defaultPrevented;
}

describe("SubmitOnceForm", () => {
  it("lets the first submit through, then drops the next ones", async () => {
    const { form, button } = await mount();
    expect(form.getAttribute("method")).toBe("post");
    expect(form.getAttribute("action")).toBe("/join/claim");
    expect(button.type).toBe("submit");
    expect(button.disabled).toBe(false);
    let prevented = true;
    await act(async () => {
      prevented = submit(form);
    });
    expect(prevented).toBe(false);
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(button.textContent).toBe("Opening…");
    await act(async () => {
      prevented = submit(form);
    });
    expect(prevented).toBe(true);
  });

  it("is ready again when the page comes back from the back-forward cache", async () => {
    const { form, button } = await mount();
    await act(async () => {
      submit(form);
    });
    await act(async () => {
      window.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: false }));
    });
    expect(button.disabled).toBe(true);
    await act(async () => {
      window.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: true }));
    });
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe("Open invitation");
    let prevented = true;
    await act(async () => {
      prevented = submit(form);
    });
    expect(prevented).toBe(false);
  });
});
