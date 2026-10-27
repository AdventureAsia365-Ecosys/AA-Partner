// web/app/trip/[tourId]/TripHeader.tsx — AA-979 P2b §4.3: the Adventure Asia header of the client-host trip page and
// its not-found page (the CIS page's brand line), plus the small upper-case label style both use. Server component.
import { K, sans, serif } from "@/app/_kit";

export const tripLabel = {
  fontFamily: sans,
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.12em",
  color: K.accent,
  fontWeight: 700,
} as const;

export function TripHeader() {
  return (
    <header
      style={{
        borderBottom: `1px solid ${K.line}`,
        padding: "20px 16px",
        display: "flex",
        flexWrap: "wrap",
        gap: 8,
        justifyContent: "space-between",
        alignItems: "center",
      }}
    >
      <span style={{ fontFamily: serif, fontWeight: 700, fontSize: 18, letterSpacing: "0.02em" }}>Adventure Asia</span>
      <span style={{ ...tripLabel, letterSpacing: "0.14em" }}>Discreet Executive Adventure</span>
    </header>
  );
}
