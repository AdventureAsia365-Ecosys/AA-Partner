// AA-979 P2b §4.3 — the client-host trip page: the CIS read (200 / 301 / 410 / 404 / faults), the page's answers
// (render, 308 to the current version, not-found, 500), its metadata (indexable) and the host rules.
import { readFileSync } from "fs";
import path from "path";
import { NextRequest } from "next/server";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SITE_URL, SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import { loadConfig } from "@/lib/config";
import { clientPathAllowed } from "@/lib/hosts";
import { itineraryParagraphs, loadTrip, parseTrip, REVALIDATE_SECONDS } from "@/lib/tripPage";
import { gate } from "@/proxy";
import { ENV, testConfig } from "./helpers";

const CIS = "https://cis-api.example.test";
const ID = "0b6f1d2e-3c4a-4b5c-8d9e-0f1a2b3c4d5e";
const NEW_ID = "1c7a2e3f-4d5b-4c6d-9e0f-1a2b3c4d5e6f";

const TRIP = {
  tour_id: ID,
  country: "Vietnam",
  duration: "8 days",
  name: "Northern Highlands",
  subtitle: "Rice terraces and markets",
  summary: "A quiet week in the north.",
  itineraries: "Day 1. Hanoi.\n\nDay 2. Sapa.",
  highlights: ["Ha Giang loop", 7, "Bac Ha market"],
  seo_title: "Northern Highlands | Adventure Asia",
  seo_meta: "Eight days in the north of Vietnam.",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function redirect(location: string, status = 301): Response {
  return new Response(null, { status, headers: { location } });
}

describe("loadTrip (CIS GET /v1/trip/{id})", () => {
  it("reads the page data with a manual redirect, a timeout and the hourly data cache", async () => {
    const fetchImpl = vi.fn(async () => json(TRIP));
    const found = await loadTrip(CIS, ID, fetchImpl);
    expect(found.kind).toBe("ok");
    if (found.kind === "ok") {
      expect(found.trip.name).toBe("Northern Highlands");
      expect(found.trip.highlights).toEqual(["Ha Giang loop", "Bac Ha market"]);
    }
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit & { next?: { revalidate?: number } }];
    expect(url).toBe(`${CIS}/v1/trip/${ID}`);
    expect(init.redirect).toBe("manual");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.next?.revalidate).toBe(REVALIDATE_SECONDS);
  });

  it("maps CIS's 301 for a superseded version to the current tour id (relative or absolute Location)", async () => {
    expect(await loadTrip(CIS, ID, async () => redirect(`/v1/trip/${NEW_ID}`))).toEqual({ kind: "moved", tourId: NEW_ID });
    expect(await loadTrip(CIS, ID, async () => redirect(`${CIS}/v1/trip/${NEW_ID.toUpperCase()}`, 308))).toEqual({
      kind: "moved",
      tourId: NEW_ID,
    });
  });

  it("never follows a redirect that does not name another tour", async () => {
    expect(await loadTrip(CIS, ID, async () => redirect("https://evil.example.test/"))).toEqual({ kind: "error" });
    expect(await loadTrip(CIS, ID, async () => redirect(`/v1/trip/${ID}`))).toEqual({ kind: "error" });
    expect(await loadTrip(CIS, ID, async () => new Response(null, { status: 301 }))).toEqual({ kind: "error" });
  });

  it("answers not_found for 410 (retired) and 404, error for faults", async () => {
    expect(await loadTrip(CIS, ID, async () => json({ detail: "gone" }, 410))).toEqual({ kind: "not_found" });
    expect(await loadTrip(CIS, ID, async () => json({ detail: "nope" }, 404))).toEqual({ kind: "not_found" });
    expect(await loadTrip(CIS, ID, async () => json({}, 502))).toEqual({ kind: "error" });
    expect(await loadTrip(CIS, ID, async () => new Response("<html>", { status: 200 }))).toEqual({ kind: "error" });
    expect(await loadTrip(CIS, ID, async () => json({ ...TRIP, tour_id: NEW_ID }))).toEqual({ kind: "error" });
    expect(
      await loadTrip(CIS, ID, async () => {
        throw new TypeError("fetch failed");
      }),
    ).toEqual({ kind: "error" });
  });

  it("refuses a tour id that is not a UUID before any request, and needs CIS_API_URL", async () => {
    const fetchImpl = vi.fn(async () => json(TRIP));
    for (const bad of ["abc", "../admin", `${ID}/../x`, `${ID}%2F`, ""]) {
      expect(await loadTrip(CIS, bad, fetchImpl)).toEqual({ kind: "not_found" });
    }
    expect(await loadTrip("", ID, fetchImpl)).toEqual({ kind: "error" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("parses only a body of the asked tour, and splits the itinerary on blank lines", () => {
    expect(parseTrip(null, ID)).toBeNull();
    expect(parseTrip({ ...TRIP, tour_id: 5 }, ID)).toBeNull();
    expect(parseTrip({ tour_id: ID.toUpperCase() }, ID)?.highlights).toEqual([]);
    expect(itineraryParagraphs("A\n\n\nB\n\n ")).toEqual(["A", "B"]);
  });
});

describe("config CIS_API_URL", () => {
  it("is an https origin or empty", () => {
    expect(loadConfig({ ...ENV, CIS_API_URL: CIS }).cisApiUrl).toBe(CIS);
    expect(loadConfig({ ...ENV, CIS_API_URL: `${CIS}/v1` }).cisApiUrl).toBe("");
    expect(loadConfig({ ...ENV, CIS_API_URL: "http://cis-api.example.test" }).cisApiUrl).toBe("");
    expect(loadConfig(ENV).cisApiUrl).toBe("");
    // Optional: it never turns sign-in off.
    expect(loadConfig(ENV).authConfigured).toBe(true);
  });
});

describe("host rules", () => {
  function req(url: string) {
    const headers = new Headers({ host: new URL(url).host });
    return new NextRequest(url, { headers });
  }
  beforeEach(() => {
    testConfig();
  });

  it("serves /trip/* on the client host only", () => {
    expect(clientPathAllowed(`/trip/${ID}`)).toBe(true);
    expect(clientPathAllowed("/tripx")).toBe(false);
    expect(gate(req(`https://go.example.test/trip/${ID}`)).headers.get("x-middleware-next")).toBe("1");
    expect(gate(req(`https://go.example.test/trip/${ID}`)).headers.get("x-middleware-request-x-aa-host-kind")).toBe("client");
    // The partner host neither serves it nor sends the visitor to sign-in for it.
    expect(gate(req(`https://partner.example.test/trip/${ID}`)).status).toBe(404);
    expect(gate(req(`https://partner.example.test/c/inquiry`)).status).toBe(404);
  });

  it("robots.txt lets crawlers read /trip/ and nothing else", () => {
    const robots = readFileSync(path.resolve(__dirname, "../public/robots.txt"), "utf8");
    const lines = robots.split("\n").map((l) => l.trim()).filter(Boolean);
    expect(lines).toEqual(["User-agent: *", "Allow: /trip/", "Disallow: /"]);
  });
});

describe("the page", () => {
  beforeEach(() => {
    testConfig({ CIS_API_URL: CIS });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function page() {
    return import("@/app/trip/[tourId]/page");
  }
  const props = (id: string) => ({ params: Promise.resolve({ tourId: id }) });

  function digest(err: unknown): string {
    return String((err as { digest?: string }).digest ?? "");
  }

  it("renders the trip and is indexable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(TRIP)));
    const { default: TripPage, generateMetadata } = await page();
    const html = renderToStaticMarkup(await TripPage(props(ID)));
    expect(html).toContain("Northern Highlands");
    expect(html).toContain("Bac Ha market");
    expect(html).toContain("Day 2. Sapa.");
    expect(html).toContain('lang="en"');
    // AA-636: AA's published address, never the adventureasia.com one (not AA's domain).
    expect(html).toContain(`mailto:${SUPPORT_EMAIL}?subject=`);
    expect(html).not.toContain("adventureasia.com");
    const meta = await generateMetadata(props(ID));
    expect(meta.title).toBe("Northern Highlands | Adventure Asia");
    expect(meta.description).toBe("Eight days in the north of Vietnam.");
    expect(meta.robots).toEqual({ index: true, follow: true });
  });

  it("answers a permanent redirect to the current version on this host", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => redirect(`/v1/trip/${NEW_ID}`)));
    const { default: TripPage } = await page();
    const err = await TripPage(props(ID)).catch((e: unknown) => e);
    expect(digest(err)).toContain("NEXT_REDIRECT");
    expect(digest(err)).toContain(`/trip/${NEW_ID}`);
    expect(digest(err)).toContain("308");
  });

  it("answers 308 to the lower-case id for an upper-case one, before any CIS read", async () => {
    const fetchImpl = vi.fn(async () => json(TRIP));
    vi.stubGlobal("fetch", fetchImpl);
    const { default: TripPage, generateMetadata } = await page();
    for (const run of [() => TripPage(props(ID.toUpperCase())), () => generateMetadata(props(ID.toUpperCase()))]) {
      const err = await run().catch((e: unknown) => e);
      expect(digest(err)).toContain("NEXT_REDIRECT");
      expect(digest(err)).toContain(`/trip/${ID};308`);
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("the trip not-found page keeps the trip header and links to adventure.asia, never to /", async () => {
    const { default: TripNotFound } = await import("@/app/trip/[tourId]/not-found");
    const html = renderToStaticMarkup(TripNotFound());
    expect(html).toContain("This journey is no longer available");
    expect(html).toContain("Adventure Asia");
    expect(html).not.toContain("Adventure Asia Partner");
    expect(html).toContain(`href="${SITE_URL}"`);
    expect(html).not.toContain('href="/"');
  });

  it("renders not-found for a retired tour (410) and an unknown one (404); noindex metadata", async () => {
    for (const status of [410, 404]) {
      vi.stubGlobal("fetch", vi.fn(async () => json({}, status)));
      const { default: TripPage, generateMetadata } = await page();
      const err = await TripPage(props(ID)).catch((e: unknown) => e);
      expect(digest(err)).toContain("404");
      expect((await generateMetadata(props(ID))).robots).toBeUndefined();
    }
  });

  it("fails with a 500 (not a false 404) when CIS is down", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({}, 503)));
    const { default: TripPage } = await page();
    await expect(TripPage(props(ID))).rejects.toThrow("trip_page_unavailable");
  });
});
