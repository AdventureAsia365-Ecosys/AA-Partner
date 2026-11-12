// web/app/trip/[tourId]/page.tsx — AA-979 P2b §4.3: the public trip page on the CLIENT host (Dev go-dev.), moved
// from the CIS admin frontend (AA-CIS-App frontend/app/trip/[tourId]/page.tsx, AA-482, ADR-2026-030 D5 "AA hosts the
// white-label tour page"). The CIS admin host answers 307 to here once this page is live (runbook §4.3 step 2).
//
// Public: no session, no agency data. Server component: reads CIS's public GET /v1/trip/{id} (lib/tripPage.ts), which
// also stamps url_alive for the T10 gate F6. A superseded version answers 308 to the current one (as the CIS page
// did), a retired or unknown tour the not-found page, a CIS fault a 500 (never a false "gone").
//
// Indexable (the CIS page was): the root layout's noindex is overridden here, and public/robots.txt allows /trip/.
//
// AA-1072 (item crm-02): a link that carries an agency's inquiry form key (`?f=<key>`, optionally `&p=<AA tour key>`)
// shows that agency's "Book now" widget (components/booking/BookNowWidget.tsx) instead of the AA contact button: the
// departures, the agency's live price, and a booking that opens the client's proposal page to accept and pay. An
// agency without Book now (its owner's switch, off by default) or a tour it cannot sell online shows "Ask <agency>"
// (its inquiry form). Words in English unless the visitor chose Vietnamese (cookie).
// AA-1094: the widget books the page's own AA catalogue key (CIS `source_id`); a `p=` key in the link only counts
// while CIS sends none, so a link can no longer put another tour's booking under this page's text.
import type { Metadata } from "next";
import { notFound, permanentRedirect } from "next/navigation";
import { cache } from "react";
import { AiNotice, K, PageFrame, sans, serif } from "@/app/_kit";
import { SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import { config } from "@/lib/config";
import { itineraryParagraphs, loadTrip, TOUR_ID, type TripLookup } from "@/lib/tripPage";
import { TripHeader, tripLabel as label } from "./TripHeader";
import { BookNowWidget } from "@/components/booking/BookNowWidget";
import { bookingLink, widgetTour } from "@/lib/bookNow";
import { dictionary } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

type Search = Promise<{ f?: string | string[]; p?: string | string[] }>;

/** Only the widget's strings go to the browser (not the agency app's dictionary). */
function bookMessages(lang: "en" | "vi"): Record<string, string> {
  return Object.fromEntries(Object.entries(dictionary(lang)).filter(([k]) => k.startsWith("book.")));
}

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

export default async function TripPage({ params, searchParams }: { params: Promise<{ tourId: string }>; searchParams?: Search }) {
  const { tourId } = await params;
  const booking = bookingLink((await searchParams) ?? {});
  redirectToLowerCase(tourId);
  const found = await lookup(tourId);
  if (found.kind === "moved") permanentRedirect(`/trip/${found.tourId}`);
  if (found.kind === "not_found") notFound();
  if (found.kind === "error") throw new Error("trip_page_unavailable");
  const { trip } = found;
  const ctx = booking ? await serverContext() : null;

  return (
    <PageFrame testId="trip-page" style={{ background: K.bg, minHeight: "100vh", color: K.ink, padding: 0, overflowY: "visible" }}>
      <div lang="en">
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

        {/* AA-1033: the summary, highlights and itinerary above are written with the help of AI (CIS pipeline). */}
        <AiNotice variant="traveller" lang="en" testId="trip-ai-notice" style={{ margin: "0 0 28px", fontSize: 13 }} />

        {booking && ctx ? (
          <div style={{ padding: "28px 0 0", borderTop: `1px solid ${K.line}` }} lang={ctx.lang}>
            {/* AA-1072: the agency's live price comes from AA's quote with the agency's own retail (a real source). */}
            <BookNowWidget
              formKey={booking.formKey}
              tour={widgetTour(trip.source_id, booking.tour)}
              pageId={trip.tour_id}
              locale={ctx.locale}
              dict={bookMessages(ctx.lang)}
            />
          </div>
        ) : (
          // No price shown — ADR-2026-030 §8.4: never display a price without a real source.
          <div style={{ textAlign: "center", padding: "36px 0 0", borderTop: `1px solid ${K.line}` }}>
            <a
              href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(
                `Design This Journey — ${trip.name ?? trip.tour_id}`,
              )}`}
              style={{
                display: "inline-block",
                background: K.accent,
                color: K.onAccent,
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
        )}
      </section>
      </div>
    </PageFrame>
  );
}
