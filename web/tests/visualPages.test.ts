// web/tests/visualPages.test.ts
// AA-1008 (root ADR 0016 decision 9, guard G16) — the agency app's visual run: its page list (e2e/visual/pages.ts), the
// changed-pages mapping and the gated kit gallery route. Run: npm test (CI: Web Build, "Unit tests").
//   1. every web/app/**/page.tsx route is listed in PAGES or in EXCLUDED with a reason, and nothing else is;
//   2. slugs are unique and file-name safe; signed-in pages render a kit frame unless they say otherwise;
//   3. changed-pages.mjs: a page folder touches its pages, a private folder its parent's, shared files every page, the
//      API, the unit tests and docs none;
//   4. selectPages: public vs signed, the gallery only in a gallery run, the planted pages only in the self-test;
//   5. the gallery route answers 404 unless the server runs with KIT_GALLERY=1.
import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EXCLUDED, PAGES, selectPages } from "../../e2e/visual/pages";
// @ts-expect-error -- a plain ES module without type declarations (it is run by node in the workflow)
import { changedSlugs } from "../../e2e/visual/changed-pages.mjs";

const APP = path.resolve(__dirname, "../app");

function appRoutes(dir = APP): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === "node_modules" || name === "api" || name === "bff") continue;
      out.push(...appRoutes(p));
    } else if (name === "page.tsx") {
      const rel = path.relative(APP, dir).split(path.sep).filter((s) => s && !/^\(.*\)$/.test(s)).join("/");
      out.push(`/${rel}`);
    }
  }
  return out;
}

describe("visual run page list (G16)", () => {
  it("lists or excludes every app route with a reason, and nothing else", () => {
    const routes = new Set(appRoutes());
    const listed = new Set(PAGES.map((p) => p.route).filter((r) => r.startsWith("/")));
    const missing = [...routes].filter((r) => !listed.has(r) && !EXCLUDED[r]);
    expect(missing, "add these routes to e2e/visual/pages.ts (or EXCLUDED with a reason)").toEqual([]);
    const stale = [...listed, ...Object.keys(EXCLUDED)].filter((r) => !routes.has(r));
    expect(stale, "these listed routes have no page.tsx any more").toEqual([]);
    for (const [r, why] of Object.entries(EXCLUDED)) expect(why.length, `${r} needs a real reason`).toBeGreaterThan(20);
    for (const r of listed) expect(EXCLUDED[r], `${r} is both listed and excluded`).toBeUndefined();
  });

  it("has unique, file-name safe slugs; signed-in pages render a kit frame", () => {
    const slugs = PAGES.map((p) => p.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
    for (const p of PAGES) {
      expect(p.slug).toMatch(/^[a-z0-9-]+$/);
      if (p.auth === "signed" && p.slug !== "not-found") expect(p.frame, `${p.slug}: the shell renders <main>`).not.toBe(false);
    }
    expect(PAGES.filter((p) => p.selftest)).toHaveLength(2);
  });
});

describe("changed-pages", () => {
  it("maps a page folder to its pages, a private folder to its parent's, shared files to every page", () => {
    expect(changedSlugs(["web/app/(crm)/bookings/page.tsx"])).toEqual(["bookings"]);
    expect(changedSlugs(["web/app/(crm)/settings/team/page.tsx"])).toEqual(["settings-team"]);
    expect(changedSlugs(["web/app/(studio)/portal/tours/mine/page.tsx"])).toEqual(["portal-tours-mine"]);
    expect(changedSlugs(["web/app/(studio)/portal/_components/SlateTab.tsx"])).toEqual(
      PAGES.filter((p) => p.route.startsWith("/portal/")).map((p) => p.slug).sort(),
    );
    expect(changedSlugs(["web/app/kit-gallery/GalleryView.tsx"])).toEqual(["kit-gallery", "selftest-document", "selftest-frame"]);
    for (const f of ["web/app/layout.tsx", "web/app/(crm)/layout.tsx", "web/app/_kit/core/Modal.tsx", "web/components/team/TeamAdmin.tsx", "web/lib/hosts.ts", "web/proxy.ts", "e2e/visual/visual.spec.ts", ".github/workflows/visual.yml"]) {
      expect(changedSlugs([f]), f).toBe("all");
    }
  });

  it("maps the API, the unit tests and docs to no page", () => {
    expect(changedSlugs(["api/app/main.py", "web/tests/team.test.ts", "docs/x.md", "e2e/tests/slice1.spec.ts"])).toEqual([]);
  });
});

describe("selectPages", () => {
  it("separates public and signed pages; the gallery only in a gallery run; the planted pages only in the self-test", () => {
    const pub = selectPages(PAGES, { auth: "public", slugs: "all", gallery: false, selftest: false }).map((p) => p.slug);
    expect(pub.sort()).toEqual(["health", "legal-partner-terms", "legal-privacy", "signed-out", "staff-signed-out"]);
    const pr = selectPages(PAGES, { auth: "public", slugs: "", gallery: true, selftest: false }).map((p) => p.slug);
    expect(pr).toEqual(["kit-gallery"]);
    const signed = selectPages(PAGES, { auth: "signed", slugs: "bookings,health", gallery: true, selftest: false }).map((p) => p.slug);
    expect(signed).toEqual(["bookings"]);
    const self = selectPages(PAGES, { auth: "public", slugs: "all", gallery: true, selftest: true }).map((p) => p.slug);
    expect(self).toEqual(["selftest-document", "selftest-frame"]);
  });
});

describe("the kit gallery route", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("answers 404 unless the server runs with KIT_GALLERY=1", async () => {
    vi.stubEnv("KIT_GALLERY", "");
    const { default: KitGalleryPage } = await import("@/app/kit-gallery/page");
    await expect(KitGalleryPage({ searchParams: Promise.resolve({}) })).rejects.toThrow();
    vi.stubEnv("KIT_GALLERY", "1");
    await expect(KitGalleryPage({ searchParams: Promise.resolve({ plant: "frame" }) })).resolves.toBeTruthy();
  });
});
