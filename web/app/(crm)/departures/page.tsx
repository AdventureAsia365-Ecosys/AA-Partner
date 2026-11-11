// web/app/(crm)/departures/page.tsx — AA-1070 (crm-22): the departure roster list. One row per Adventure Asia departure
// the agency has travellers on (from its AA lines), with the dates, the state the Booking feed sent (scheduled /
// guaranteed / cancelled), the trips, pax by type and how many trips have Stage 3 complete; a row opens the departure
// (its roster, the merged manifest and, for the owner and agents, its group costs). Every role.
import { EmptyState, ErrorState } from "@/app/_kit";
import { FilterTabs } from "@/components/FilterTabs";
import { PageTitle } from "@/components/PageTitle";
import { ReportTable } from "@/components/reports/ReportTable";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import { departurePath, paxTotal, type DepartureRow } from "@/lib/travelOps";

export const dynamic = "force-dynamic";

const SCOPES = ["upcoming", "past", "all"] as const;
type Scope = (typeof SCOPES)[number];

export default async function DeparturesPage({ searchParams }: { searchParams: Promise<{ scope?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  const { session } = await serverContext();
  if (!session) return null;
  const { scope: raw } = await searchParams;
  const scope: Scope = (SCOPES as readonly string[]).includes(raw ?? "") ? (raw as Scope) : "upcoming";
  const view = await crmCall<{ departures: DepartureRow[] }>(ctx.cfg, session, "GET", "departures", { query: new URLSearchParams({ scope }) });
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const { locale } = ctx;
  const rows = view.data.departures;
  return (
    <div data-testid="crm-departures" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={tr("nav.departures")} subtitle={tr("travel.departures.subtitle")} />
      <FilterTabs
        tabs={SCOPES.map((s) => ({ key: s, label: tr(`travel.departures.scope.${s}`), href: `/departures?scope=${s}` }))}
        active={scope}
        ariaLabel={tr("travel.departures.scope")}
        testId="departures-scope"
      />
      {rows.length === 0 ? (
        <EmptyState title={tr("travel.departures.empty")} description={tr("travel.departures.emptyBody")} />
      ) : (
        <ReportTable
          tableId="departures"
          head={[tr("travel.col.departure"), tr("travel.col.state"), tr("travel.col.trips"), tr("travel.col.pax"), tr("travel.col.stage3")]}
          rows={rows.map((d) => ({
            key: d.departure_id,
            href: departurePath(d.departure_id),
            cells: [
              [d.title, d.service_from && formatDate(d.service_from, locale, { year: true })].filter(Boolean).join(" · "),
              tr(`travel.departure.state.${d.state.status}`),
              String(d.trips),
              fmt(tr("travel.paxSplit"), { total: String(paxTotal(d.pax)), adults: String(d.pax.adults), children: String(d.pax.children), infants: String(d.pax.infants) }),
              `${d.stage3_complete} / ${d.trips}`,
            ],
          }))}
          empty={tr("travel.departures.empty")}
        />
      )}
    </div>
  );
}
