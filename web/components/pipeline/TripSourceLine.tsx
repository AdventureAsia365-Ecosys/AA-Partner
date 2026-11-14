// web/components/pipeline/TripSourceLine.tsx — AA-1089 (Odoo study rank 48): the trip header's line naming the AA tour
// and departure the trip came from (GET /trips/{id} `aa_source`): the tour title, the departure date, "Booked online"
// when "Book now" on the public tour page made the trip, and a link to that page (a new tab) while AA serves the tour.
// Kit components only; no personal data (a tour title and a date).
import { Badge, K } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import { aaSourceHref, type AaSourceView } from "@/lib/pipeline";

export function TripSourceLine({ source, lang, locale }: { source?: AaSourceView | null; lang: Lang; locale: DateLocale }) {
  if (!source) return null; // the trip names no AA tour (or an older API)
  const tr = (key: MessageKey) => t(lang, key);
  const href = aaSourceHref(source);
  return (
    <div
      data-testid="trip-aa-source"
      data-origin={source.origin}
      style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 13, color: K.muted }}
    >
      <Badge tone="neutral">{tr("tripSource.label")}</Badge>
      <span data-testid="trip-aa-source-title" style={{ color: K.ink, overflowWrap: "anywhere" }}>
        {source.title ?? tr("tripSource.unknownTour")}
      </span>
      {source.departure_date && (
        <span data-testid="trip-aa-source-departure">{fmt(tr("tripSource.departs"), { date: formatDate(source.departure_date, locale) })}</span>
      )}
      {source.origin === "web_booking" && <Badge tone="accent">{tr("tripSource.bookedOnline")}</Badge>}
      {href && (
        <a data-testid="trip-aa-source-link" href={href} target="_blank" rel="noopener noreferrer" title={tr("tripSource.pageHint")} style={{ color: K.accentDeep }}>
          {tr("tripSource.page")} ↗
        </a>
      )}
    </div>
  );
}
