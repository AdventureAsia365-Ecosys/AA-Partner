// web/app/trip/[tourId]/page.tsx — AA-979 P2b §4.3: the public trip page on the CLIENT host (Dev go-dev.), moved
// from the CIS admin frontend (AA-CIS-App frontend/app/trip/[tourId]/page.tsx, AA-482, ADR-2026-030 D5 "AA hosts the
// white-label tour page"). The CIS admin host answers 307 to here once this page is live (runbook §4.3 step 2).
//
// Public: no session, no agency data. Server component: reads CIS's public GET /v1/trip/{id} (lib/tripPage.ts), which
// also stamps url_alive for the T10 gate F6. A superseded version answers 308 to the current one (as the CIS page
// did), a retired or unknown tour the not-found page, a CIS fault a 500 (never a false "gone").
//
// Indexable (the CIS page was): the root layout's noindex is overridden here, and public/robots.txt allows /trip/.
import type { Metadata } from "next";
import { notFound, permanentRedirect } from "next/navigation";
import { cache } from "react";
import { K, sans, serif } from "@/app/_kit";
import { SUPPORT_EMAIL } from "@/app/_brand/tokens";
import { config } from "@/lib/config";
import { itineraryParagraphs, loadTrip, TOUR_ID, type TripLookup } from "@/lib/tripPage";
import { TripHeader, tripLabel as label } from "./TripHeader";

// One CIS read per request for the metadata and the page.
const lookup = cache((tourId: string): Promise<TripLookup> => loadTrip(config().cisApiUrl, tourId));

// One URL per trip: a tour id with upper-case hex answers 308 to its lower-case form before any CIS read, so the
// indexable page never has two addresses.
function redirectToLowerCase(tourId: string): void {
  if (TOUR_ID.test(tourId) && tourId !== tourId.toLowerCase()) permanentRedirect(`/trip/${tourId.toLowerCase()}`);
}

export async function generateMetadata({ params }: { params: Promise<{ tourId: string }> }): Promise<Metadata> {
  const { tourId } = await params;
  redirectToLowerCase(tourId);
  const found = await lookup(tourId);
  if (found.kind !== "ok") return { title: "Trip Not Found — Adventure Asia" };
  const { trip } = found;
  return {
    title: trip.seo_title || trip.name || "Adventure Asia",
    description: trip.seo_meta || trip.summary || undefined,
    robots: { index: true, follow: true },
  };
}

export default async function TripPage({ params }: { params: Promise<{ tourId: string }> }) {
  const { tourId } = await params;
  redirectToLowerCase(tourId);
  const found = await lookup(tourId);
  if (found.kind === "moved") permanentRedirect(`/trip/${found.tourId}`);
  if (found.kind === "not_found") notFound();
  if (found.kind === "error") throw new Error("trip_page_unavailable");
  const { trip } = found;

  return (
    <main lang="en" data-testid="trip-page" style={{ background: K.bg, minHeight: "100vh", color: K.ink }}>
      <TripHeader />

      <section style={{ maxWidth: 760, margin: "0 auto", padding: "56px 16px 80px" }}>
        {trip.country && (
          <div
            style={{
              fontFamily: sans,
              fontSize: 12,
              textTransform: "uppercase",
              letterSpacing: "0.14em",
              color: K.muted,
              marginBottom: 14,
            }}
          >
            {trip.country}
            {trip.duration ? ` · ${trip.duration}` : ""}
          </div>
        )}
        <h1
          data-testid="trip-name"
          style={{ fontFamily: serif, fontSize: 38, lineHeight: 1.2, margin: "0 0 12px", fontWeight: 700, overflowWrap: "anywhere" }}
        >
          {trip.name}
        </h1>
        {trip.subtitle && (
          <p style={{ fontFamily: serif, fontSize: 18, color: K.ink3, fontStyle: "italic", margin: "0 0 32px", lineHeight: 1.5 }}>
            {trip.subtitle}
          </p>
        )}
        {trip.summary && (
          <p style={{ fontSize: 16.5, lineHeight: 1.8, color: K.ink2, margin: "0 0 36px" }}>{trip.summary}</p>
        )}

        {trip.highlights.length > 0 && (
          <div
            style={{
              margin: "0 0 40px",
              padding: "24px 28px",
              background: K.card,
              border: `1px solid ${K.line}`,
              borderRadius: 6,
            }}
          >
            <div style={{ ...label, marginBottom: 14 }}>Journey Highlights</div>
            <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
              {trip.highlights.map((h, i) => (
                <li key={i} style={{ display: "flex", gap: 10, marginBottom: 10, fontSize: 15, lineHeight: 1.6, color: K.ink2 }}>
                  <span style={{ color: K.accent, flexShrink: 0 }}>—</span>
                  <span>{h}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {trip.itineraries && (
          <div style={{ marginBottom: 40 }}>
            <div style={{ ...label, marginBottom: 18 }}>The Itinerary</div>
            {itineraryParagraphs(trip.itineraries).map((block, i) => (
              <p key={i} style={{ margin: "0 0 18px", lineHeight: 1.75, color: K.ink2, fontSize: 15.5 }}>
                {block}
              </p>
            ))}
          </div>
        )}

        {/* No price shown — ADR-2026-030 §8.4: never display a price without a real source. */}
        <div style={{ textAlign: "center", padding: "36px 0 0", borderTop: `1px solid ${K.line}` }}>
          <a
            href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(
              `Design This Journey — ${trip.name ?? trip.tour_id}`,
            )}`}
            style={{
              display: "inline-block",
              background: K.accent,
              color: "#fff",
              fontFamily: sans,
              fontWeight: 700,
              fontSize: 14,
              letterSpacing: "0.02em",
              padding: "14px 32px",
              borderRadius: 999,
              textDecoration: "none",
            }}
          >
            Design This Journey
          </a>
        </div>
      </section>
    </main>
  );
}
