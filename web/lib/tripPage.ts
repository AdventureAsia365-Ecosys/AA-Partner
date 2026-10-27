// web/lib/tripPage.ts
// AA-979 P2b §4.3 — the public trip page on the CLIENT host (/trip/<tourId>), moved from the CIS admin frontend
// (AA-CIS-App frontend/app/trip/[tourId]/page.tsx, AA-482; root ADR 0015 Q5 for the 301 / 410 rules).
//
// The data is CIS's public, no-token GET <CIS_API_URL>/v1/trip/{tourId} (AA-CIS-App api/routers/v1_trip_page.py):
//
//   200  the page data (and CIS stamps url_alive on its tenant_tour_pages row, the T10 gate F6 reads it)
//   301  an older version of a tour whose catalog key is served by a newer one: Location /v1/trip/<current id>;
//        the page answers a permanent redirect to /trip/<current id> on this host, as the CIS page did
//   410  a tour Adventure Asia no longer offers; 404 never served or paused. Both render the not-found page
//        (a Next.js page cannot answer 410), as the CIS page did
//
// Anything else (CIS 5xx, timeout, CIS_API_URL unset) is "error": the page throws, so the visitor gets a 500 that a
// crawler retries, instead of a 404 that tells it the trip is gone.

export const TOUR_ID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const MOVED_LOCATION = /\/v1\/trip\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/;
const FETCH_TIMEOUT_MS = 5_000;
/** Trip content changes rarely: the Next data cache keeps an answer for an hour (same as the CIS page). */
export const REVALIDATE_SECONDS = 3600;

export interface TripPageData {
  tour_id: string;
  country: string | null;
  duration: string | null;
  name: string | null;
  subtitle: string | null;
  summary: string | null;
  itineraries: string | null;
  highlights: string[];
  seo_title: string | null;
  seo_meta: string | null;
}

export type TripLookup =
  | { kind: "ok"; trip: TripPageData }
  | { kind: "moved"; tourId: string }
  | { kind: "not_found" }
  | { kind: "error" };

type FetchLike = (input: string, init?: RequestInit & { next?: { revalidate?: number } }) => Promise<Response>;

function text(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/** The answer's fields, typed; null when the body is not a trip page of the asked tour. */
export function parseTrip(body: unknown, tourId: string): TripPageData | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  if (typeof b.tour_id !== "string" || b.tour_id.toLowerCase() !== tourId.toLowerCase()) return null;
  return {
    tour_id: b.tour_id,
    country: text(b.country),
    duration: text(b.duration),
    name: text(b.name),
    subtitle: text(b.subtitle),
    summary: text(b.summary),
    itineraries: text(b.itineraries),
    highlights: Array.isArray(b.highlights) ? b.highlights.filter((h): h is string => typeof h === "string") : [],
    seo_title: text(b.seo_title),
    seo_meta: text(b.seo_meta),
  };
}

/**
 * Read one trip page from CIS. `tourId` must be a UUID (anything else is not_found before any request, as the CIS
 * API does), `cisApiUrl` the CIS API origin (empty = error: the web is not configured for trip pages).
 */
export async function loadTrip(cisApiUrl: string, tourId: string, fetchImpl: FetchLike = fetch): Promise<TripLookup> {
  if (!TOUR_ID.test(tourId)) return { kind: "not_found" };
  if (!cisApiUrl) return { kind: "error" };
  let res: Response;
  try {
    res = await fetchImpl(`${cisApiUrl}/v1/trip/${tourId}`, {
      redirect: "manual",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      next: { revalidate: REVALIDATE_SECONDS },
    });
  } catch {
    return { kind: "error" };
  }
  if ([301, 302, 307, 308].includes(res.status)) {
    const m = (res.headers.get("location") ?? "").match(MOVED_LOCATION);
    // A redirect that does not name another tour is not one CIS sends: treat it as a fault, never follow it.
    if (!m || m[1].toLowerCase() === tourId.toLowerCase()) return { kind: "error" };
    return { kind: "moved", tourId: m[1].toLowerCase() };
  }
  if (res.status === 404 || res.status === 410) return { kind: "not_found" };
  if (!res.ok) return { kind: "error" };
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return { kind: "error" };
  }
  const trip = parseTrip(body, tourId);
  return trip ? { kind: "ok", trip } : { kind: "error" };
}

/** The itinerary text as paragraphs (blank lines separate them). */
export function itineraryParagraphs(textValue: string): string[] {
  return textValue.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
}
