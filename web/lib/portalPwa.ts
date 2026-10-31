// web/lib/portalPwa.ts
// AA-954 (card CRM-24b, R16) — the browser side of the installable traveller page: register the service worker
// (lib/portalServiceWorker.ts, scope /c/t), hand it the page shell to keep once a traveller is signed in, tell it to
// drop every portal cache, and the install prompt state. Every call is guarded: no service worker support, an insecure
// context (http on a LAN address) or a blocked registration leave the page working online as before.

import { SERVICE_WORKER_PATH, SERVICE_WORKER_SCOPE, PORTAL_PAGE } from "./portalServiceWorker";

type Nav = Pick<Navigator, "serviceWorker"> | undefined;
const READY_WAIT_MS = 5000;

function workerApi(nav: Nav = typeof navigator !== "undefined" ? navigator : undefined): ServiceWorkerContainer | null {
  try {
    if (typeof window !== "undefined" && !window.isSecureContext) return null;
    return nav && "serviceWorker" in nav && nav.serviceWorker ? nav.serviceWorker : null;
  } catch {
    return null;
  }
}

export async function registerPortalWorker(nav?: Nav): Promise<ServiceWorkerRegistration | null> {
  const sw = workerApi(nav);
  if (!sw) return null;
  try {
    return await sw.register(SERVICE_WORKER_PATH, { scope: SERVICE_WORKER_SCOPE, updateViaCache: "none" });
  } catch {
    return null;
  }
}

/**
 * The page and the static files this page loaded (resource timing), which is what an offline start of the installed
 * page needs. Only same-origin /_next/static/* files; the worker checks the list again.
 */
export function shellUrls(entries: readonly { name: string }[], origin: string): string[] {
  const out = new Set<string>([PORTAL_PAGE]);
  for (const e of entries) {
    try {
      const u = new URL(e.name, origin);
      if (u.origin === origin && u.pathname.startsWith("/_next/static/")) out.add(u.pathname + u.search);
    } catch {
      // not a URL
    }
  }
  return [...out];
}

async function post(message: unknown, nav?: Nav): Promise<void> {
  const sw = workerApi(nav);
  if (!sw) return;
  try {
    // The first visit registers the worker and signs in within seconds: wait (a little) until it is active.
    const reg = await Promise.race([sw.ready, new Promise<null>((done) => setTimeout(() => done(null), READY_WAIT_MS))]);
    (reg?.active ?? sw.controller)?.postMessage(message);
  } catch {
    // no worker yet: the next signed-in load sends it again
  }
}

let shellSent = false;

/** After a signed-in load: let the worker keep this page and its static files (replacing what it kept before). Once
 * per page load (every save reloads the home; the shell does not change in between), again after a clear. */
export async function precacheShell(nav?: Nav): Promise<void> {
  if (typeof window === "undefined" || typeof performance === "undefined" || shellSent) return;
  shellSent = true;
  const entries = performance.getEntriesByType("resource") as PerformanceResourceTiming[];
  await post({ type: "precache", urls: shellUrls(entries, window.location.origin) }, nav);
}

/** At sign-out (the page deletes the caches itself too; this also reaches a worker that is mid-precache). */
export async function clearWorkerCaches(nav?: Nav): Promise<void> {
  shellSent = false;
  await post({ type: "clear" }, nav);
}

/** The `beforeinstallprompt` event (Chromium); not in the DOM typings. */
export interface InstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export type InstallMode = "installed" | "prompt" | "ios" | "none";

/**
 * Which install help to show: already running as an installed app -> nothing to do; the browser offered its prompt
 * -> our button; iPhone / iPad Safari (no prompt event there) -> the "Share, then Add to Home Screen" hint; anything
 * else -> nothing (the browser's own menu still installs it).
 */
export function installMode(input: { standalone: boolean; prompt: boolean; userAgent: string; installed: boolean }): InstallMode {
  if (input.standalone || input.installed) return "installed";
  if (input.prompt) return "prompt";
  if (/iPhone|iPad|iPod/i.test(input.userAgent)) return "ios";
  return "none";
}

export function runningStandalone(): boolean {
  try {
    if (typeof window === "undefined") return false;
    const nav = navigator as Navigator & { standalone?: boolean };
    return window.matchMedia?.("(display-mode: standalone)").matches === true || nav.standalone === true;
  } catch {
    return false;
  }
}
