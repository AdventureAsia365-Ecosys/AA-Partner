// @vitest-environment jsdom
// AA-954 (card CRM-24b, R16; design v3 C-09, §2.9 rule 7 = Fix A2) — the installable, offline traveller page:
//   - card R: a cache inspection after a signed-in load finds no document, Stage 3, F1, flight, group or payment field
//     (only the allow-listed itinerary, built by copying named fields);
//   - card E: offline, the page shows the last synced itinerary with "last updated";
//   - the reconnect rule (server wins) and the clear rules (sign-out, a gone link, another link's answer, expiry);
//   - the service worker keeps the page shell only: it never answers or stores an API call, a document, the signed-link
//     landing or another origin;
//   - installable: the manifest (start URL and scope /c/t, icons that exist), the routes, the client-host routing.
// Every name, place and address here is invented.
import fs from "node:fs";
import path from "node:path";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as manifestRoute } from "@/app/c/portal.webmanifest/route";
import { GET as workerRoute } from "@/app/c/sw.js/route";
import { clientMessages } from "@/lib/clientMessages";
import { PORTAL_API } from "@/lib/clientPortal";
import { clientOnlyPath, clientPathAllowed } from "@/lib/hosts";
import { dictionary } from "@/lib/i18n";
import {
  OFFLINE_DATA_CACHE,
  OFFLINE_DATA_KEY,
  offlineCopy,
  parseStored,
  readOffline,
  reconcile,
  syncOffline,
  usable,
} from "@/lib/offlineItinerary";
import { manifestHref, portalManifest } from "@/lib/portalManifest";
import { installMode, shellUrls } from "@/lib/portalPwa";
import { PORTAL_PAGE, SERVICE_WORKER_PATH, SERVICE_WORKER_SCOPE, SERVICE_WORKER_SOURCE, SHELL_CACHE } from "@/lib/portalServiceWorker";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = new Date("2026-12-03T08:00:00Z");
const REF = "0123456789abcdef0123456789abcdef";

// ── A Cache Storage in memory (jsdom has none) ──────────────────────────────────────────────────────────────────
class MemCache {
  entries = new Map<string, { body: string; type: string; status: number }>();
  async match(key: string | Request): Promise<Response | undefined> {
    const e = this.entries.get(typeof key === "string" ? key : new URL(key.url).pathname);
    return e ? new Response(e.body, { status: e.status, headers: { "Content-Type": e.type } }) : undefined;
  }
  async put(key: string, res: Response): Promise<void> {
    this.entries.set(key, { body: await res.text(), type: res.headers.get("content-type") ?? "", status: res.status });
  }
}

class MemStorage {
  stores = new Map<string, MemCache>();
  async open(name: string): Promise<MemCache> {
    if (!this.stores.has(name)) this.stores.set(name, new MemCache());
    return this.stores.get(name)!;
  }
  async delete(name: string): Promise<boolean> {
    return this.stores.delete(name);
  }
  async keys(): Promise<string[]> {
    return [...this.stores.keys()];
  }
  async has(name: string): Promise<boolean> {
    return this.stores.has(name);
  }
  async match(key: string, opts: { cacheName?: string } = {}): Promise<Response | undefined> {
    const names = opts.cacheName ? [opts.cacheName] : [...this.stores.keys()];
    for (const n of names) {
      const hit = await this.stores.get(n)?.match(key);
      if (hit) return hit;
    }
    return undefined;
  }
  /** Everything stored, as text: what a cache inspection reads. */
  dump(): string {
    return [...this.stores.entries()].map(([n, c]) => `${n}\n${[...c.entries.entries()].map(([k, e]) => `${k}\n${e.body}`).join("\n")}`).join("\n");
  }
}

function asStorage(m: MemStorage): CacheStorage {
  return m as unknown as CacheStorage;
}

// ── A signed-in home with a sentinel in every field the device must never keep ──────────────────────────────────
const SENTINELS = {
  stage3: "SENTINEL-STAGE3-passport_expiry",
  waiver: "SENTINEL-WAIVER-BODY",
  envelope: "SENTINEL-ENVELOPE-TERMS",
  document: "SENTINEL-DOC-voucher-ilse.pdf",
  flight: "QX9271",
  airport: "ZQN",
  me: "Ilse Brandvold",
  companion: "Teo Brandvold",
  amount: "4321.09",
  futureF1: "X7731942",
  confirmClass: "SENTINEL-CONFIRM-CLASS",
  dayExtra: "SENTINEL-DAY-EXTRA",
};

const SIGNED_IN_HOME = {
  agency_name: "Lantern Tours",
  signed_in: true,
  role: "lead",
  offline_ref: REF,
  link_expires_at: "2026-12-20T10:00:00+00:00",
  agency_contact: { email: "desk@lantern-tours.example" },
  trip: { title: "Fjord coast 6 nights", start_date: "2026-12-01", end_date: "2026-12-07", stage: "booked" },
  stage3: { locked: false, fields: ["passport_no", "passport_expiry"], missing: [SENTINELS.stage3], done: false, submitted_at: null },
  waiver: { doc_id: "w1", version_label: "v3", body_text: SENTINELS.waiver, body_sha256: "ab", signed: false, signed_at: null },
  agreements: { documents: [{ kind: "booking_terms", doc_id: "t1", version_label: "v1", body_text: SENTINELS.envelope, body_sha256: "cd" }], insurance_declination: null, envelopes: [], guardian_signs_for_me: false },
  me: { traveler_id: "00000000-0000-4000-8000-000000000b01", name: SENTINELS.me, role: "lead" },
  switch_to: [{ traveler_id: "00000000-0000-4000-8000-000000000b02", name: SENTINELS.companion, role: "companion" }],
  flights: [{ direction: "arrival", flight_no: SENTINELS.flight, airport: SENTINELS.airport, local_time: "2026-11-30T22:10", local_date: "2026-11-30", tz: "Pacific/Auckland", utc_offset: "+13:00", days_from_start: -1 }],
  documents: [{ document_id: "d1", kind: "voucher", filename: SENTINELS.document, content_type: "application/pdf", size_bytes: 2048, for: "me", created_at: "2026-11-01T10:00:00+00:00" }],
  services: [{ title: "Glacier boat day", from: "2026-12-02", to: null, status: "confirmed", confirm_class: SENTINELS.confirmClass, pay_on_arrival: true, price: SENTINELS.amount }],
  itinerary: [
    { day_no: 0, before_you_go: true, date: null, title: "Pack layers", place: null, tz: "UTC", utc_offset: "+00:00" },
    { day_no: 1, before_you_go: false, date: "2026-12-01", title: "Arrive in Harbourview", place: "Harbourview", tz: "Pacific/Auckland", utc_offset: "+13:00", note: SENTINELS.dayExtra },
    { day_no: 2, before_you_go: false, date: "2026-12-02", title: "Glacier boat", place: "Mistfall Sound", tz: "Pacific/Auckland", utc_offset: "+13:00" },
  ],
  passport_scan_on_file: true,
  passport_number: SENTINELS.futureF1, // a field a later API might add: never copied
  party: [{ traveler_id: "00000000-0000-4000-8000-000000000b02", name: SENTINELS.companion, role: "companion", details_done: false, waiver_signed: null, me: false }],
  payments: [{ kind: "balance", amount: SENTINELS.amount, currency: "NZD", due_on: "2026-11-20", status: "due" }],
};

const FORBIDDEN_KEYS = ["stage3", "waiver", "agreements", "documents", "flights", "flight_no", "party", "payments", "passport", "me", "switch_to", "role", "name", "price", "confirm_class_value"];

describe("card R: a cache inspection finds only the itinerary", () => {
  it("a signed-in load stores the allow-listed itinerary and nothing else", async () => {
    const cs = new MemStorage();
    const kept = await syncOffline({ status: 200, home: SIGNED_IN_HOME }, NOW, asStorage(cs));
    expect(kept?.days.map((d) => d.title)).toEqual(["Pack layers", "Arrive in Harbourview", "Glacier boat"]);
    const dump = cs.dump();
    for (const [what, value] of Object.entries(SENTINELS)) expect(dump, what).not.toContain(value);
    const copy = parseStored((cs.stores.get(OFFLINE_DATA_CACHE)!.entries.get(OFFLINE_DATA_KEY)!).body)!;
    expect(Object.keys(copy).sort()).toEqual(["agency_name", "contact", "days", "expires_at", "ref", "saved_at", "services", "trip", "v"]);
    const keysIn = (v: unknown): string[] =>
      Array.isArray(v) ? v.flatMap(keysIn) : v && typeof v === "object" ? Object.entries(v).flatMap(([k, x]) => [k, ...keysIn(x)]) : [];
    for (const k of FORBIDDEN_KEYS) expect(keysIn(copy), k).not.toContain(k);
    expect(copy.contact).toEqual({ email: "desk@lantern-tours.example" });
    expect(copy.services).toEqual([{ title: "Glacier boat day", from: "2026-12-02", to: null, status: "confirmed", confirm_class: null, pay_on_arrival: true }]);
    expect(copy.saved_at).toBe(NOW.toISOString());
    expect(copy.expires_at).toBe("2026-12-20T10:00:00+00:00");
  });

  it("only one entry exists for the whole origin, and a signed-out home stores nothing", async () => {
    const cs = new MemStorage();
    await syncOffline({ status: 200, home: { agency_name: "Lantern Tours", signed_in: false, offline_ref: REF } }, NOW, asStorage(cs));
    expect(await cs.keys()).toEqual([]);
    await syncOffline({ status: 200, home: SIGNED_IN_HOME }, NOW, asStorage(cs));
    await syncOffline({ status: 200, home: { ...SIGNED_IN_HOME, trip: { ...SIGNED_IN_HOME.trip, title: "Fjord coast 7 nights" } } }, NOW, asStorage(cs));
    expect([...cs.stores.get(OFFLINE_DATA_CACHE)!.entries.keys()]).toEqual([OFFLINE_DATA_KEY]);
    expect((await readOffline(asStorage(cs)))?.trip.title).toBe("Fjord coast 7 nights"); // server wins
  });
});

describe("the reconnect and clear rules", () => {
  const stored = offlineCopy(SIGNED_IN_HOME, NOW)!;
  const signedOut = (ref: string) => ({ status: 200, home: { agency_name: "Lantern Tours", signed_in: false, offline_ref: ref } });

  it("server wins: a signed-in answer replaces the copy (another person of the group too)", () => {
    const other = { ...SIGNED_IN_HOME, itinerary: [{ day_no: 1, before_you_go: false, date: "2026-12-01", title: "Kayak", place: "Inlet", tz: "UTC", utc_offset: "+00:00" }] };
    const action = reconcile(stored, { status: 200, home: other }, NOW);
    expect(action.kind === "save" && action.copy.days[0].title).toBe("Kayak");
  });

  it("a signed-out answer keeps this link's copy and clears another link's (another agency or traveller)", () => {
    expect(reconcile(stored, signedOut(REF), NOW).kind).toBe("keep");
    expect(reconcile(stored, signedOut("f".repeat(32)), NOW).kind).toBe("clear");
    expect(reconcile(stored, { status: 200, home: { signed_in: false } }, NOW).kind).toBe("clear");
    expect(reconcile(null, signedOut(REF), NOW).kind).toBe("keep");
  });

  it("a gone link clears; no network, a rate limit or a server fault keeps", () => {
    expect(reconcile(stored, { status: 404 }, NOW).kind).toBe("clear");
    for (const status of [0, 429, 500, 502]) expect(reconcile(stored, { status }, NOW).kind).toBe("keep");
  });

  it("an expired link's copy is not shown and is cleared on the next signed-out answer", () => {
    const later = new Date("2026-12-21T00:00:00Z");
    expect(usable(stored, NOW)).toBe(true);
    expect(usable(stored, later)).toBe(false);
    expect(reconcile(stored, signedOut(REF), later).kind).toBe("clear");
  });

  it("a signed-in answer of an API without the offline fields keeps nothing", () => {
    const { offline_ref: _r, ...old } = SIGNED_IN_HOME;
    void _r;
    expect(offlineCopy(old, NOW)).toBeNull();
    expect(reconcile(stored, { status: 200, home: old }, NOW).kind).toBe("clear");
  });

  it("unreadable or older stored entries count as nothing", () => {
    expect(parseStored("not json")).toBeNull();
    expect(parseStored(JSON.stringify({ ...stored, v: 0 }))).toBeNull();
    expect(parseStored(JSON.stringify(stored))?.ref).toBe(REF);
  });
});

// ── The service worker, run in a fake worker scope ──────────────────────────────────────────────────────────────
type Handler = (event: Record<string, unknown>) => void;

function worker(network: (url: string, init?: RequestInit) => Promise<Response>) {
  const handlers: Record<string, Handler> = {};
  const cs = new MemStorage();
  const fetched: string[] = [];
  const scope = {
    location: { origin: "https://go.example" },
    addEventListener: (type: string, h: Handler) => (handlers[type] = h),
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(async () => undefined) },
  };
  const fetchFn = async (input: string | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.url;
    fetched.push(url);
    return network(url, init);
  };
  new Function("self", "caches", "fetch", SERVICE_WORKER_SOURCE)(scope, cs, fetchFn);

  async function dispatch(type: string, fields: Record<string, unknown>): Promise<{ responded: Response | null }> {
    let waited: Promise<unknown> = Promise.resolve();
    let responded: Promise<Response> | null = null;
    handlers[type]({ ...fields, waitUntil: (p: Promise<unknown>) => (waited = p), respondWith: (p: Promise<Response>) => (responded = p) });
    await waited;
    return { responded: responded ? await responded : null };
  }
  const request = (url: string, mode = "cors", method = "GET") => ({ url: new URL(url, "https://go.example").href, mode, method });
  return { cs, fetched, scope, dispatch, request };
}

function html(body: string): Response {
  const res = new Response(body, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
  Object.defineProperty(res, "type", { value: "basic" });
  return res;
}

function asset(body: string): Response {
  const res = new Response(body, { status: 200, headers: { "Content-Type": "text/javascript" } });
  Object.defineProperty(res, "type", { value: "basic" });
  return res;
}

describe("the service worker keeps the page shell only", () => {
  it("precache accepts only /c/t and same-origin /_next/static files, and replaces the shell as a whole", async () => {
    const w = worker(async (url) => (url === PORTAL_PAGE ? html("<p>shell</p>") : asset(`// ${url}`)));
    await w.dispatch("message", {
      data: {
        type: "precache",
        urls: [
          "/c/t",
          "/_next/static/chunks/app-1a2b.js",
          "/_next/static/css/kit-3c4d.css?dpl=x",
          `${PORTAL_API}`, // the API home
          `${PORTAL_API}/documents/d1`, // a document
          "/c/t/" + "k".repeat(43), // the signed-link landing
          "/c/t?next=1",
          "https://cdn.other.example/_next/static/x.js", // another origin
          "/_next/static/../../crm/public/v1/traveller",
        ],
      },
    });
    expect([...w.cs.stores.get(SHELL_CACHE)!.entries.keys()].sort()).toEqual(["/_next/static/chunks/app-1a2b.js", "/_next/static/css/kit-3c4d.css?dpl=x", "/c/t"]);
    expect(w.fetched.some((u) => u.includes("/crm/") || u.includes("other.example") || u.startsWith("/c/t/"))).toBe(false);
    // A later precache replaces it (old deploys' files never pile up).
    await w.dispatch("message", { data: { type: "precache", urls: ["/c/t", "/_next/static/chunks/app-9z.js"] } });
    expect([...w.cs.stores.get(SHELL_CACHE)!.entries.keys()].sort()).toEqual(["/_next/static/chunks/app-9z.js", "/c/t"]);
  });

  it("precache without the page itself, or with a failed or non-HTML page, keeps nothing new", async () => {
    const w = worker(async (url) => (url === PORTAL_PAGE ? asset("not html") : asset("x")));
    await w.dispatch("message", { data: { type: "precache", urls: ["/_next/static/a.js"] } });
    await w.dispatch("message", { data: { type: "precache", urls: ["/c/t", "/_next/static/a.js"] } });
    expect(w.cs.stores.has(SHELL_CACHE)).toBe(false);
  });

  it("fetch: /c/t is network first with the shell offline; API, documents, the landing and writes are never touched", async () => {
    let online = true;
    const w = worker(async (url) => {
      if (!online) throw new TypeError("Failed to fetch");
      return url.endsWith("/c/t") ? html("<p>fresh</p>") : asset("x");
    });
    expect(await (await w.dispatch("fetch", { request: w.request("/c/t", "navigate") })).responded!.text()).toBe("<p>fresh</p>");
    online = false;
    const none = (await w.dispatch("fetch", { request: w.request("/c/t", "navigate") })).responded!;
    expect(none.status).toBe(503); // nothing kept yet: the small offline page
    expect(await none.text()).toContain("You are offline");
    (await w.cs.open(SHELL_CACHE)).entries.set("/c/t", { body: "<p>kept</p>", type: "text/html", status: 200 });
    expect(await (await w.dispatch("fetch", { request: w.request("/c/t", "navigate") })).responded!.text()).toBe("<p>kept</p>");
    for (const req of [
      w.request(PORTAL_API),
      w.request(`${PORTAL_API}/documents/d1`),
      w.request("/c/t/" + "k".repeat(43), "navigate"),
      w.request(`${PORTAL_API}/sign-out`, "cors", "POST"),
      w.request("https://fonts.other.example/css"),
      w.request("/c/p", "navigate"),
    ]) {
      expect((await w.dispatch("fetch", { request: req })).responded, req.url).toBeNull();
    }
  });

  it("fetch: a kept static file is served from the shell, else from the network", async () => {
    const w = worker(async () => asset("network"));
    (await w.cs.open(SHELL_CACHE)).entries.set("/_next/static/chunks/a.js", { body: "kept", type: "text/javascript", status: 200 });
    expect(await (await w.dispatch("fetch", { request: w.request("/_next/static/chunks/a.js") })).responded!.text()).toBe("kept");
    expect(await (await w.dispatch("fetch", { request: w.request("/_next/static/chunks/b.js") })).responded!.text()).toBe("network");
  });

  it("activate drops older portal caches only; clear drops every portal cache", async () => {
    const w = worker(async () => asset("x"));
    for (const n of ["aa-portal-shell-v0", "aa-portal-data-v0", SHELL_CACHE, OFFLINE_DATA_CACHE, "someone-else"]) await w.cs.open(n);
    await w.dispatch("activate", {});
    expect((await w.cs.keys()).sort()).toEqual([OFFLINE_DATA_CACHE, SHELL_CACHE, "someone-else"].sort());
    expect(w.scope.clients.claim).toHaveBeenCalled();
    await w.dispatch("message", { data: { type: "clear" } });
    expect(await w.cs.keys()).toEqual(["someone-else"]);
  });

  it("the route serves it as JavaScript that the browser re-checks, from /c/ for the scope /c/t", async () => {
    const res = workerRoute();
    expect(res.headers.get("content-type")).toContain("text/javascript");
    expect(res.headers.get("cache-control")).toBe("no-cache");
    expect(await res.text()).toBe(SERVICE_WORKER_SOURCE);
    expect(SERVICE_WORKER_PATH).toBe("/c/sw.js");
    expect(SERVICE_WORKER_SCOPE.startsWith(path.posix.dirname(SERVICE_WORKER_PATH))).toBe(true);
    expect(SERVICE_WORKER_SOURCE).toContain(JSON.stringify(OFFLINE_DATA_CACHE)); // the names are the page's
  });
});

describe("installable", () => {
  it("the manifest: start URL and scope /c/t, standalone, icons that exist, both languages", async () => {
    const en = portalManifest("en");
    expect(en).toMatchObject({ id: "/c/t", start_url: "/c/t", scope: "/c/t", display: "standalone", name: "My trip" });
    expect(en.icons.map((i) => i.sizes)).toEqual(["192x192", "512x512", "512x512"]);
    for (const icon of en.icons) {
      const file = path.join(__dirname, "..", "public", icon.src);
      expect(fs.existsSync(file), icon.src).toBe(true);
      const png = fs.readFileSync(file);
      expect(png.subarray(1, 4).toString()).toBe("PNG");
      expect(`${png.readUInt32BE(16)}x${png.readUInt32BE(20)}`).toBe(icon.sizes);
    }
    expect(portalManifest("vi").name).toBe("Chuyến đi của tôi");
    expect(manifestHref("vi")).toBe("/c/portal.webmanifest?lang=vi");
    const res = manifestRoute(new Request("https://go.example/c/portal.webmanifest?lang=vi"));
    expect(res.headers.get("content-type")).toContain("application/manifest+json");
    expect((await res.json()).lang).toBe("vi");
  });

  it("the client host serves the worker, the manifest and the icons; the partner host does not", () => {
    for (const p of ["/c/sw.js", "/c/portal.webmanifest", "/c/icons/trip-192.png"]) {
      expect(clientPathAllowed(p), p).toBe(true);
      expect(clientOnlyPath(p), p).toBe(true);
    }
  });

  it("which install help shows", () => {
    const ua = { android: "Mozilla/5.0 (Linux; Android 14) Chrome/130", iphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)" };
    expect(installMode({ standalone: true, prompt: true, userAgent: ua.android, installed: false })).toBe("installed");
    expect(installMode({ standalone: false, prompt: true, userAgent: ua.android, installed: false })).toBe("prompt");
    expect(installMode({ standalone: false, prompt: false, userAgent: ua.iphone, installed: false })).toBe("ios");
    expect(installMode({ standalone: false, prompt: false, userAgent: ua.android, installed: false })).toBe("none");
  });

  it("the shell list: the page and this origin's static files only", () => {
    expect(
      shellUrls(
        [{ name: "https://go.example/_next/static/chunks/a.js" }, { name: `https://go.example${PORTAL_API}` }, { name: "https://fonts.other.example/x.woff2" }, { name: "::" }],
        "https://go.example",
      ),
    ).toEqual(["/c/t", "/_next/static/chunks/a.js"]);
  });

  it("every offline and install string exists in EN and VI and reaches the client page", () => {
    const en = dictionary("en") as Record<string, string>;
    const vi = dictionary("vi") as Record<string, string>;
    const keys = Object.keys(en).filter((k) => /^client\.(offline|install|manifest)\.|^client\.error\.offline$/.test(k));
    expect(keys.length).toBeGreaterThanOrEqual(17);
    const sent = clientMessages("vi") as Record<string, string>;
    for (const k of keys) {
      expect(vi[k] && vi[k] !== en[k], k).toBeTruthy();
      expect(sent[k], k).toBe(vi[k]);
    }
  });
});

// ── The page ────────────────────────────────────────────────────────────────────────────────────────────────────
describe("the portal page offline (card E)", () => {
  let root: Root | null = null;
  let cs: MemStorage;
  beforeEach(() => {
    cs = new MemStorage();
    vi.stubGlobal("caches", cs);
  });
  afterEach(() => {
    if (root) act(() => root!.unmount());
    root = null;
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  async function mount(): Promise<HTMLElement> {
    const { PortalPage } = await import("@/components/portal/PortalPage");
    const el = document.createElement("div");
    document.body.appendChild(el);
    root = createRoot(el);
    await act(async () => {
      root!.render(createElement(PortalPage, { base: PORTAL_API, dict: clientMessages("en"), locale: "en-AU", offline: true }));
    });
    await act(async () => new Promise((r) => setTimeout(r, 0)));
    return el;
  }

  function reply(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  it("offline shows the last synced itinerary with 'last updated' and the agency contact; online again replaces it", async () => {
    await syncOffline({ status: 200, home: SIGNED_IN_HOME }, new Date(), asStorage(cs));
    let online = false;
    const fresh = { ...SIGNED_IN_HOME, trip: { ...SIGNED_IN_HOME.trip, title: "Fjord coast 7 nights" } };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        if (!online) throw new TypeError("Failed to fetch");
        return reply(200, fresh);
      }),
    );
    const el = await mount();
    const view = el.querySelector('[data-testid="portal-offline"]')!;
    expect(view.getAttribute("data-saved")).toBe("true");
    expect(view.textContent).toContain("Fjord coast 6 nights");
    expect(el.querySelector('[data-testid="portal-offline-updated"]')?.textContent).toMatch(/^Saved on this device\. Last updated .+\.$/);
    expect(el.querySelector('[data-testid="portal-day-1"]')?.textContent).toContain("Harbourview");
    expect(el.querySelector<HTMLAnchorElement>('[data-testid="portal-offline-email"]')?.href).toBe("mailto:desk@lantern-tours.example");
    for (const value of Object.values(SENTINELS)) expect(view.textContent).not.toContain(value);
    online = true;
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    await act(async () => new Promise((r) => setTimeout(r, 0)));
    expect(el.querySelector('[data-testid="portal-home"]')).not.toBeNull();
    expect((await readOffline(asStorage(cs)))?.trip.title).toBe("Fjord coast 7 nights");
    expect(el.querySelector('[data-testid="portal-offline-kept"]')).not.toBeNull();
  });

  it("offline with nothing saved says so; 'Remove from this device' drops the copy", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    let el = await mount();
    expect(el.querySelector('[data-testid="portal-offline"]')?.getAttribute("data-saved")).toBe("false");
    act(() => root!.unmount());
    root = null;
    document.body.innerHTML = "";
    await syncOffline({ status: 200, home: SIGNED_IN_HOME }, new Date(), asStorage(cs));
    el = await mount();
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-offline-forget"]')!.click());
    await act(async () => new Promise((r) => setTimeout(r, 0)));
    expect(await cs.keys()).toEqual([]);
    expect(el.querySelector('[data-testid="portal-offline"]')?.getAttribute("data-saved")).toBe("false");
    expect(el.querySelector('[data-testid="portal-note"]')?.textContent).toBe("Your itinerary was removed from this device.");
  });

  it("an expired link's copy is never shown offline", async () => {
    await syncOffline({ status: 200, home: { ...SIGNED_IN_HOME, link_expires_at: "2020-01-01T00:00:00+00:00" } }, new Date("2019-12-31T00:00:00Z"), asStorage(cs));
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const el = await mount();
    expect(el.querySelector('[data-testid="portal-offline"]')?.getAttribute("data-saved")).toBe("false");
    expect(await cs.keys()).toEqual([]);
  });

  it("sign-out leaves nothing of the traveller on the device; a gone link clears too", async () => {
    let signedIn = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        if (init.method === "POST" && url.endsWith("/sign-out")) {
          signedIn = false;
          return reply(200, { signed_in: false });
        }
        return reply(200, signedIn ? SIGNED_IN_HOME : { agency_name: "Lantern Tours", signed_in: false, offline_ref: REF });
      }),
    );
    const el = await mount();
    expect((await readOffline(asStorage(cs)))?.ref).toBe(REF);
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-sign-out"]')!.click());
    await act(async () => new Promise((r) => setTimeout(r, 0)));
    expect(el.querySelector('[data-testid="portal-sign-in"]')).not.toBeNull();
    expect(await cs.keys()).toEqual([]);

    await syncOffline({ status: 200, home: SIGNED_IN_HOME }, new Date(), asStorage(cs));
    vi.stubGlobal("fetch", vi.fn(async () => reply(404, { detail: "link_not_found" })));
    act(() => root!.unmount());
    root = null;
    await mount();
    expect(await cs.keys()).toEqual([]);
  });

  it("a write while offline says the page is offline", async () => {
    let online = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        if (!online && init.method !== "GET") throw new TypeError("Failed to fetch");
        return reply(200, SIGNED_IN_HOME);
      }),
    );
    const el = await mount();
    online = false;
    await act(async () => {
      window.dispatchEvent(new Event("offline"));
    });
    expect(el.querySelector('[data-testid="portal-offline-banner"]')).not.toBeNull();
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-sign-out"]')!.click());
    expect(el.querySelector('[data-testid="portal-note"]')?.textContent).toBe("You are offline. Please try again when you are connected.");
  });
});
