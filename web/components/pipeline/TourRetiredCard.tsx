// web/components/pipeline/TourRetiredCard.tsx — AA-1093 part 1: the trip page's card when Adventure Asia no longer
// offers an AA tour the trip uses (GET /trips/{id} `aa_tour_retired`, the open crm-063 flags). Information only:
// nothing was changed on the proposal options, holds or bookings; the task "AA tour discontinued" asks the agent to
// act. Kit components only; no personal data (the tour title, counts and a date).
import { Badge, K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import { tourRetiredLines, type TourRetiredView } from "@/lib/pipeline";

export function TourRetiredCard({
  tours,
  lang,
  locale,
  timeZone,
}: {
  tours: TourRetiredView[];
  lang: Lang;
  locale: DateLocale;
  timeZone: string;
}) {
  const tr = (key: MessageKey) => t(lang, key);
  return (
    <Panel title={tr("tourRetired.title")} testId="trip-aa-tour-retired">
      <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
        <p style={{ margin: 0, color: K.muted }}>{tr("tourRetired.help")}</p>
        {tours.map((tour) => (
          <div key={tour.source_id} data-testid="aa-tour-retired-tour" style={{ display: "grid", gap: 6 }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <Badge tone="warning">{tr("tourRetired.badge")}</Badge>
              <span style={{ color: K.ink, overflowWrap: "anywhere" }}>{tour.title ?? tr("tourRetired.unknownTour")}</span>
              <span style={{ color: K.muted }}>{fmt(tr("tourRetired.since"), { date: formatInstant(tour.raised_at, locale, timeZone) })}</span>
            </div>
            <ul style={{ margin: 0, paddingLeft: 18, color: K.body }}>
              {tourRetiredLines(tour).map(({ kind, n }) => (
                <li key={kind} data-kind={kind}>
                  {fmt(tr(`tourRetired.item.${kind}` as MessageKey), { n })}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Panel>
  );
}
