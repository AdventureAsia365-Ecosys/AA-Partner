// web/app/(crm)/travelling/page.tsx — A-02 Travelling now (AA-961, card CRM-30; design v3 R37, F9): the agency's
// booked, pre-departure and travelling trips in a window (today / 7 / 14 days), grouped by region; where each trip is
// (day number, the day plan's place, the AA or agency services running), the open AA incident flags with the
// affected trips, and per region "Reach travellers here" (names hidden until a logged reveal; card E: a traveller
// on two trips is one row). Every role (the API checks).
import { Badge, EmptyState, ErrorState, K } from "@/app/_kit";
import { OpenIncidents } from "@/components/duty/IncidentPanel";
import { ReachList } from "@/components/duty/ReachList";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { WINDOWS, regionLabel, type TravellingView } from "@/lib/duty";
import { fmt, plural, t, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function TravellingPage({ searchParams }: { searchParams: Promise<{ days?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const { days: raw } = await searchParams;
  const days = WINDOWS.find((w) => String(w) === raw) ?? 7;
  const view = await crmCall<TravellingView>(ctx.cfg, session, "GET", "duty/travelling", {
    query: new URLSearchParams({ days: String(days) }),
  });
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const { regions, travellers, incidents } = view.data;
  const titles = Object.fromEntries(regions.flatMap((g) => g.trips.map((trip) => [trip.trip_id, trip.title])));
  return (
    <div data-testid="crm-travelling" style={{ display: "grid", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={tr("duty.title")} subtitle={tr("duty.subtitle")} />
      <nav style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 13 }} aria-label={tr("duty.window")}>
        {WINDOWS.map((w) => (
          <a key={w} href={`/travelling?days=${w}`} aria-current={w === days ? "page" : undefined}>
            {tr(`duty.window.${w}`)}
          </a>
        ))}
      </nav>
      <OpenIncidents incidents={incidents} lang={ctx.lang} locale={ctx.locale} />
      {regions.length === 0 ? (
        <EmptyState title={tr("duty.empty.title")} description={tr("duty.empty.body")} />
      ) : (
        regions.map((group) => {
          const inRegion = new Set(group.trips.flatMap((trip) => trip.traveler_ids));
          return (
            <Panel
              key={group.region}
              testId={`duty-region-${group.region}`}
              title={`${regionLabel(group.region, tr)} · ${plural(ctx.lang, "duty.travellers", group.traveller_count)}`}
            >
              <ul style={{ listStyle: "none", margin: "0 0 12px", padding: 0, display: "grid", gap: 10 }}>
                {group.trips.map((trip) => (
                  <li
                    key={trip.trip_id}
                    data-testid={`duty-trip-${trip.trip_id}`}
                    style={{ borderTop: `1px solid ${K.line}`, paddingTop: 8, display: "grid", gap: 4, minWidth: 0 }}
                  >
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                      <a href={`/pipeline/${trip.trip_id}`} style={{ fontWeight: 600, overflowWrap: "anywhere" }}>
                        {trip.title}
                      </a>
                      <Badge tone={trip.stage === "travelling" ? "success" : "info"}>{tr(`duty.stage.${trip.stage}`)}</Badge>
                      {trip.incident_flag_id && <Badge tone="danger">{tr("duty.trip.incident")}</Badge>}
                      {trip.is_mine && <Badge dot={false}>{tr("duty.trip.mine")}</Badge>}
                    </div>
                    <div style={{ fontSize: 12.5, color: K.muted }}>
                      {formatDate(trip.start_date, ctx.locale)} – {formatDate(trip.end_date, ctx.locale)}
                      {" · "}
                      {trip.day_no !== null
                        ? fmt(tr("duty.trip.day"), { n: trip.day_no })
                        : fmt(tr("duty.trip.leaves"), { date: formatDate(trip.start_date, ctx.locale) })}
                      {trip.place && ` · ${trip.place}`}
                      {" · "}
                      {plural(ctx.lang, "duty.travellers", trip.traveler_ids.length)}
                    </div>
                    {trip.lines.length > 0 && (
                      <div style={{ fontSize: 12.5 }}>
                        <span style={{ color: K.muted }}>{tr("duty.trip.lines")}: </span>
                        {trip.lines.map((line) => line.title).join(", ")}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
              <details>
                <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>{tr("duty.reach")}</summary>
                <div style={{ marginTop: 10 }}>
                  <ReachList
                    travellers={travellers.filter((p) => inRegion.has(p.traveler_id))}
                    tripTitles={titles}
                    lang={ctx.lang}
                  />
                </div>
              </details>
            </Panel>
          );
        })
      )}
      {view.data.truncated && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("duty.truncated")}</p>}
    </div>
  );
}
