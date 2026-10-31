// web/lib/portalServiceWorker.ts
// AA-954 (card CRM-24b, R16; design v3 C-09, §2.9 rule 7) — the traveller portal's service worker, served by
// app/c/sw.js/route.ts on the client host and registered by components/portal/PortalPwa.tsx with scope /c/t (the
// script sits in /c/, so that scope is allowed without a Service-Worker-Allowed header).
//
// It keeps the PAGE SHELL only, so the installed page opens without a network: the server-rendered /c/t (which holds
// the UI strings and the date locale, never a traveller's data: the page loads everything else from the API in the
// browser) and the /_next/static files that page loaded. The itinerary itself is written by the page
// (lib/offlineItinerary.ts, an allow-listed copy); this worker never reads, writes or answers an API request, a
// document, the signed-link landing /c/t/<token>, or any other origin. Every request it does not handle goes to the
// network untouched.
//
//   fetch      navigation to exactly /c/t: network first, the cached shell when offline (else a small offline page);
//              GET /_next/static/*: the cached copy when there is one (hashed, immutable files), else the network;
//   message    {type: "precache", urls}: the page, once a traveller is signed in, sends its own URL and the static
//              files it loaded; only /c/t and /_next/static/* of this origin are accepted, and the shell is replaced
//              as a whole (so it never grows across deploys); {type: "clear"}: drop every portal cache. Messages
//              run one at a time in arrival order, so a clear always lands after a precache already in flight;
//   activate   claim the open pages and drop caches of older versions (names under the "aa-portal-" prefix).
//
// Plain ES2017 in a string (no bundler for a worker in this app), so tests/offlinePwa.test.ts runs it against a fake
// worker scope. Keep the cache names in step with lib/offlineItinerary.ts (the test checks it).

import { OFFLINE_CACHE_PREFIX, OFFLINE_DATA_CACHE } from "./offlineItinerary";

export const SHELL_CACHE = `${OFFLINE_CACHE_PREFIX}shell-v1`;
export const PORTAL_PAGE = "/c/t";
export const SERVICE_WORKER_PATH = "/c/sw.js";
export const SERVICE_WORKER_SCOPE = PORTAL_PAGE;
/** The most files one precache message may name (the portal page loads a few dozen). */
export const MAX_PRECACHE = 200;

const OFFLINE_HTML =
  '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
  "<title>Offline</title></head><body style=\"font-family:system-ui,sans-serif;padding:32px 16px;max-width:440px;margin:auto;color:#1F2933;background:#F8F6F2\">" +
  "<p>You are offline. Open your trip page again when you are connected.</p>" +
  '<p lang="vi">Bạn đang ngoại tuyến. Hãy mở lại trang chuyến đi khi có kết nối.</p></body></html>';

export const SERVICE_WORKER_SOURCE = `"use strict";
const PREFIX = ${JSON.stringify(OFFLINE_CACHE_PREFIX)};
const SHELL = ${JSON.stringify(SHELL_CACHE)};
const DATA = ${JSON.stringify(OFFLINE_DATA_CACHE)};
const PAGE = ${JSON.stringify(PORTAL_PAGE)};
const MAX = ${MAX_PRECACHE};
const OFFLINE_HTML = ${JSON.stringify(OFFLINE_HTML)};

function shellPath(raw) {
  let u;
  try { u = new URL(raw, self.location.origin); } catch (e) { return null; }
  if (u.origin !== self.location.origin) return null;
  if (u.pathname === PAGE && !u.search && !u.hash) return PAGE;
  if (u.pathname.startsWith("/_next/static/") && !u.pathname.includes("..") && !u.hash) return u.pathname + u.search;
  return null;
}

async function precache(urls) {
  if (!Array.isArray(urls)) return;
  const wanted = Array.from(new Set(urls.map(shellPath).filter(Boolean))).slice(0, MAX);
  if (!wanted.includes(PAGE)) return;
  const got = [];
  for (const path of wanted) {
    const res = await fetch(path, { credentials: "same-origin", cache: path === PAGE ? "no-store" : "default" });
    if (!res.ok || res.type !== "basic" || res.redirected) return;
    if (path === PAGE && !(res.headers.get("content-type") || "").startsWith("text/html")) return;
    got.push([path, res]);
  }
  await caches.delete(SHELL);
  const cache = await caches.open(SHELL);
  for (const [path, res] of got) await cache.put(path, res);
}

async function clearAll() {
  const names = await caches.keys();
  await Promise.all(names.filter((n) => n.startsWith(PREFIX)).map((n) => caches.delete(n)));
}

function offlinePage() {
  return new Response(OFFLINE_HTML, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

self.addEventListener("install", () => { self.skipWaiting(); });

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n.startsWith(PREFIX) && n !== SHELL && n !== DATA).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

// One message at a time: a "clear" (sign-out) that arrives while a precache is still fetching runs after it, so the
// precache can never write the shell back once the caches were cleared.
let queue = Promise.resolve();
function enqueue(work) {
  queue = queue.then(work).catch(() => undefined);
  return queue;
}

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "precache") event.waitUntil(enqueue(() => precache(data.urls)));
  else if (data.type === "clear") event.waitUntil(enqueue(clearAll));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate" && url.pathname === PAGE && !url.search) {
    event.respondWith(fetch(req).catch(async () => (await caches.match(PAGE, { cacheName: SHELL })) || offlinePage()));
    return;
  }
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith((async () => (await caches.match(url.pathname + url.search, { cacheName: SHELL })) || fetch(req))());
  }
});
`;
