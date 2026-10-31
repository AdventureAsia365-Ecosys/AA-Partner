// @vitest-environment jsdom
// S262 (AA-957 live evidence): on some trip pages the dark theme was lost after hydration (data-theme="dark" at
// DOMContentLoaded from the restore script, "light" once React was done, no console error). A server/client text
// difference anywhere on the page makes React 19 render the whole document again on the client; taking over <html>
// it drops the attributes the restore script set and writes the layout's own props. This test builds that document,
// shows the theme is lost without the keeper, and kept with app/_kit/local/ThemeKeeper; and it pins the layout: no
// data-theme rendered by the server, the keeper mounted.
import { readFileSync } from "node:fs";
import path from "node:path";
import { act, createElement, type ReactNode } from "react";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";
import { ThemeKeeper } from "@/app/_kit/local/ThemeKeeper";
import { THEME_KEY } from "@/app/_kit/local/theme";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let side: "server" | "client" = "server";

/** A time printed differently by the server and the browser (the trip page's task due time before the fix). */
function Mismatch() {
  return createElement("p", { id: "due" }, side === "server" ? "due 1 Nov, 06:30" : "due 31 Oct, 23:30");
}

function page(keeper: boolean): ReactNode {
  return createElement(
    "html",
    { lang: "en", suppressHydrationWarning: true },
    createElement("head", null),
    createElement("body", null, keeper ? createElement(ThemeKeeper) : null, createElement(Mismatch)),
  );
}

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  localStorage.clear();
  side = "server";
});

/** Serve the page, run the restore script's effect (dark stored), then hydrate it in the "browser". */
async function load(keeper: boolean): Promise<string | null> {
  side = "server";
  const html = renderToString(page(keeper));
  const inner = /<html[^>]*>([\s\S]*)<\/html>/.exec(html)![1];
  for (const name of document.documentElement.getAttributeNames()) document.documentElement.removeAttribute(name);
  document.documentElement.innerHTML = inner;
  document.documentElement.setAttribute("lang", "en");
  localStorage.setItem(THEME_KEY, "dark");
  document.documentElement.setAttribute("data-theme", "dark"); // what the restore script does before paint
  side = "client";
  await act(async () => {
    roots.push(hydrateRoot(document, page(keeper), { onRecoverableError: () => {} }));
  });
  expect(document.getElementById("due")!.textContent).toBe("due 31 Oct, 23:30"); // React rebuilt the document
  return document.documentElement.getAttribute("data-theme");
}

describe("the theme survives a client re-render of the document", () => {
  it("is lost without the keeper (the defect)", async () => {
    expect(await load(false)).not.toBe("dark");
  });

  it("stays dark with the keeper", async () => {
    expect(await load(true)).toBe("dark");
  });

  it("the root layout renders no data-theme of its own and mounts the keeper", () => {
    const layout = readFileSync(path.resolve(__dirname, "../app/layout.tsx"), "utf8");
    const htmlTag = /<html lang=\{[^>]*>/.exec(layout)![0]; // the JSX element, not the comments
    expect(htmlTag).not.toContain("data-theme");
    expect(htmlTag).toContain("suppressHydrationWarning");
    expect(layout).toContain("<ThemeKeeper />");
    expect(layout).toContain("themeStore.restoreScript");
  });
});
