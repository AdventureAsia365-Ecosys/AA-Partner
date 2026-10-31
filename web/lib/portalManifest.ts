// web/lib/portalManifest.ts
// AA-954 (card CRM-24b, R16; design v3 C-09) — the web app manifest of the installable traveller page, served by
// app/c/portal.webmanifest/route.ts. Start URL and scope /c/t (the page the signed link lands on after the cookie
// exchange), so the installed app opens the traveller's own page with the cookies of this host. The agency is the brand
// of the client pages (§2.9 rule 3) and one client host serves every agency, so the name is generic ("My trip") in the
// visitor's language (?lang=vi, set by the page: a manifest is fetched without cookies). Icons: the app's own mark
// (tools/pwa/pwa_icons.py), under /c/ so the client host serves them.
import { offlineEn, offlineVi } from "./i18nOffline";
import { PORTAL_PAGE } from "./portalServiceWorker";

export const MANIFEST_PATH = "/c/portal.webmanifest";
/** --aa-ink and --aa-bg of the kit's light palette (a manifest needs literal colours). */
export const THEME_COLOR = "#1F2933";
export const BACKGROUND_COLOR = "#F8F6F2";
export const APPLE_TOUCH_ICON = "/c/icons/trip-apple-180.png";

export function manifestHref(lang: "en" | "vi"): string {
  return lang === "vi" ? `${MANIFEST_PATH}?lang=vi` : MANIFEST_PATH;
}

export function portalManifest(lang: "en" | "vi") {
  const m = lang === "vi" ? offlineVi : offlineEn;
  return {
    id: PORTAL_PAGE,
    name: m["client.manifest.name"],
    short_name: m["client.manifest.name"],
    description: m["client.manifest.description"],
    lang,
    start_url: PORTAL_PAGE,
    scope: PORTAL_PAGE,
    display: "standalone",
    background_color: BACKGROUND_COLOR,
    theme_color: THEME_COLOR,
    icons: [
      { src: "/c/icons/trip-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/c/icons/trip-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/c/icons/trip-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
