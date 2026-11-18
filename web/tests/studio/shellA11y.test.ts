// AA-1131 (S283 UI audit, batch K1) — the studio shell parts that are app code, not kit core:
//   1. the portal sidebar's Collapse row is pinned: the <aside> does not scroll, the nav does (min-height 0), so with
//      several groups open on a short window the Collapse row stays in view (as in the Booking and CIS shells);
//   2. the PageHeader layout rules are the kit's (app/_kit/core/PageHeaderStyles.tsx PAGE_HEADER_CSS), not a copy in
//      studio.css.
// Source checks (no DOM): the layout itself is measured by the visual run.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const read = (p: string) => readFileSync(path.resolve(__dirname, "../..", p), "utf8");

describe("studio shell (AA-1131)", () => {
  it("scrolls the sidebar nav, not the whole aside, and keeps Collapse outside it", () => {
    const src = read("app/_kit/local/studio/PortalSidebar.tsx");
    const aside = src.slice(src.indexOf("<aside"), src.indexOf("{/* Brand"));
    expect(aside).toMatch(/overflow: "hidden"/);
    expect(aside).not.toMatch(/overflowY: "auto"/);
    const navStart = src.indexOf("<nav");
    const nav = src.slice(navStart, src.indexOf(">\n", src.indexOf("gap: 16", navStart)));
    expect(nav).toMatch(/minHeight: 0/);
    expect(nav).toMatch(/overflowY: "auto"/);
    const footer = src.indexOf("{/* Collapse (pinned");
    expect(footer).toBeGreaterThan(src.indexOf("</nav>"));
    expect(src.slice(footer, footer + 200)).toMatch(/flex: "none"/);
    expect(src.slice(footer)).toMatch(/data-pshell="collapse-btn"/);
  });

  it("takes the PageHeader layout from the kit, not from a studio.css copy", () => {
    const css = read("app/(studio)/studio.css");
    expect(css).not.toMatch(/^\.aa-page-header-text\s*\{/m);
    expect(css).not.toMatch(/\.aa-page-header--stack\s*>/);
    expect(read("app/_kit/core/PageHeaderStyles.tsx")).toMatch(/PAGE_HEADER_CSS/);
    expect(read("app/_kit/core/primitives.tsx")).toMatch(/<PageHeaderStyles \/>/);
  });
});
