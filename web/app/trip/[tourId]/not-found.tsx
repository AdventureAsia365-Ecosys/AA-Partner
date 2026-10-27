// web/app/trip/[tourId]/not-found.tsx — AA-979 P2b §4.3: what a traveller sees for a retired (CIS 410) or unknown
// trip on the client host. Not the root not-found page: that one is the agency app's ("Adventure Asia Partner", a
// link to /), and / is 404 on the client host. This one keeps the trip page's header and links to adventure.asia.
import { K, sans, serif } from "@/app/_kit";
import { SITE_URL } from "@/app/_kit/core/brand";
import { TripHeader } from "./TripHeader";

export default function TripNotFound() {
  return (
    <main lang="en" data-testid="trip-not-found" style={{ background: K.bg, minHeight: "100vh", color: K.ink }}>
      <TripHeader />
      <section style={{ maxWidth: 760, margin: "0 auto", padding: "56px 16px 80px", textAlign: "center" }}>
        <h1 style={{ fontFamily: serif, fontSize: 30, lineHeight: 1.25, margin: "0 0 12px", fontWeight: 700 }}>
          This journey is no longer available
        </h1>
        <p style={{ fontFamily: sans, fontSize: 15.5, lineHeight: 1.7, color: K.ink2, margin: "0 0 28px" }}>
          It may have been retired or replaced. Our other journeys are on Adventure Asia.
        </p>
        <a href={SITE_URL} style={{ fontFamily: sans, fontWeight: 700, fontSize: 14, color: K.accent }}>
          Explore Adventure Asia
        </a>
      </section>
    </main>
  );
}
