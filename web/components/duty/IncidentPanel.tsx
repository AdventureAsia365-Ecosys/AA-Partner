// web/components/duty/IncidentPanel.tsx — the AA incident banner (AA-961, card CRM-30; design v3 F9.3 "an AA
// incident shows a banner"): on Travelling now, every open flag with the agency's affected trips; on the trip
// workspace, the flags on that trip's departures (open, and cleared in the last 14 days). Codes only: Booking never
// sends traveller data or free text. Server-safe (no hooks).
import { Badge, K } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { incidentDeparture, type OpenIncident, type TripIncident } from "@/lib/duty";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const box: React.CSSProperties = {
  border: `1px solid ${K.dangerBorder}`,
  background: K.dangerSoft,
  borderRadius: 10,
  padding: "12px 14px",
  display: "grid",
  gap: 8,
  minWidth: 0,
};

function code(lang: Lang, c: string): string {
  return t(lang, `duty.incident.code.${c}` as MessageKey);
}

/** "<departure> · " when the agency's line on it names it; nothing otherwise (never the Booking departure id). */
function departurePrefix(f: OpenIncident | TripIncident, lang: Lang, locale: DateLocale): string {
  const words = incidentDeparture(f.departure, locale, (key) => t(lang, key));
  return words ? `${words} · ` : "";
}

export function OpenIncidents({ incidents, lang, locale }: { incidents: OpenIncident[]; lang: Lang; locale: DateLocale }) {
  if (incidents.length === 0) return null;
  return (
    <section role="alert" data-testid="duty-incidents" style={box}>
      <strong style={{ color: K.danger, fontSize: 14 }}>
        {fmt(t(lang, "duty.incident.bannerMany"), { n: incidents.length })}
      </strong>
      <p style={{ margin: 0, fontSize: 13, color: K.ink }}>{t(lang, "duty.incident.next")}</p>
      <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 6, fontSize: 13 }}>
        {incidents.map((f) => (
          <li key={f.incident_flag_id} data-testid={`duty-incident-${f.departure_id}`}>
            <Badge tone="danger">{code(lang, f.incident_code)}</Badge>{" "}
            <span style={{ color: K.muted }}>
              {departurePrefix(f, lang, locale)}
              {fmt(t(lang, "duty.incident.raised"), { date: formatDate(f.raised_at, locale) })}
            </span>
            <div style={{ marginTop: 2 }}>
              {t(lang, "duty.incident.affected")}:{" "}
              {f.trips.map((trip, i) => (
                <span key={trip.trip_id}>
                  {i > 0 && ", "}
                  <a href={`/pipeline/${trip.trip_id}`}>{trip.title}</a>
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function TripIncidentBanner({ incidents, lang, locale }: { incidents: TripIncident[]; lang: Lang; locale: DateLocale }) {
  if (incidents.length === 0) return null;
  const open = incidents.filter((f) => f.status === "open");
  return (
    <section
      role={open.length > 0 ? "alert" : "status"}
      data-testid="trip-incident-banner"
      style={open.length > 0 ? box : { ...box, border: `1px solid ${K.line}`, background: K.card }}
    >
      <strong style={{ color: open.length > 0 ? K.danger : K.muted, fontSize: 14 }}>{t(lang, "duty.incident.banner")}</strong>
      <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4, fontSize: 13 }}>
        {incidents.map((f) => (
          <li key={f.incident_flag_id}>
            <Badge tone={f.status === "open" ? "danger" : "neutral"}>{code(lang, f.incident_code)}</Badge>{" "}
            <span style={{ color: K.muted }}>
              {departurePrefix(f, lang, locale)}
              {f.status === "open"
                ? fmt(t(lang, "duty.incident.raised"), { date: formatDate(f.raised_at, locale) })
                : fmt(t(lang, "duty.incident.cleared"), { date: formatDate(f.cleared_at, locale) })}
            </span>
          </li>
        ))}
      </ul>
      {open.length > 0 && <p style={{ margin: 0, fontSize: 13 }}>{t(lang, "duty.incident.next")}</p>}
    </section>
  );
}
