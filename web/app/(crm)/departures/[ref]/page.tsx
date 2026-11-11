// web/app/(crm)/departures/[ref]/page.tsx — AA-1068 (crm-32): one group departure (an AA departure id or the agency's
// own group code): the costs the agency carries for the whole group (its tour leader, a group flight block, a welcome
// dinner: per departure, split by travellers, or per traveller), the trips on it with their pax, their share and their
// own profit, and the totals (GET /departures/{ref}/pnl). Reached from a trip's Money card and the Home "today" tiles.
// AA-1070: an AA departure's roster (trips, travellers, checks, the merged manifest) heads the page for every role;
// the costs and P&L stay owner and agent only (the API refuses the assistant).
import { notFound } from "next/navigation";
import { ErrorState, K, KpiTile } from "@/app/_kit";
import { DepartureCosts } from "@/components/finance/DepartureCosts";
import { DepartureRoster } from "@/components/travelOps/DepartureRoster";
import { Panel, PageTitle } from "@/components/PageTitle";
import { ReportTable } from "@/components/reports/ReportTable";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { canSeeMoney, type DeparturePnl } from "@/lib/finance";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatMoney, type SuppliersView } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const REF = /^[A-Za-z0-9:_.-]{1,80}$/;

export default async function DeparturePage({ params }: { params: Promise<{ ref: string }> }) {
  const ref = decodeURIComponent((await params).ref);
  if (!REF.test(ref)) notFound();
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!canSeeMoney(ctx.role)) {
    // AA-1070: the roster is every role's; the group costs and P&L are the owner's and the agents'.
    return (
      <div data-testid="crm-departure" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1200 }}>
        <PageTitle title={fmt(tr("departure.title"), { ref })} subtitle={tr("travel.departure.subtitle")} />
        <DepartureRoster departureRef={ref} lang={ctx.lang} locale={ctx.locale} />
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const [view, suppliers] = await Promise.all([
    crmCall<DeparturePnl>(ctx.cfg, session, "GET", `departures/${encodeURIComponent(ref)}/pnl`),
    crmCall<SuppliersView>(ctx.cfg, session, "GET", "suppliers"),
  ]);
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const d = view.data;
  const { locale } = ctx;
  const money = (v: string | null | undefined) => formatMoney(v, d.base_currency, locale);
  const margin = Number(d.totals.margin_base);
  return (
    <div data-testid="crm-departure" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={fmt(tr("departure.title"), { ref })} subtitle={tr("departure.subtitle")} />
      <DepartureRoster departureRef={ref} lang={ctx.lang} locale={locale} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
        <KpiTile size="sm" testId="departure-pax" label={tr("departure.total.pax")} value={String(d.totals.pax)} />
        <KpiTile size="sm" testId="departure-cost" label={tr("departure.total.cost")} value={money(d.totals.departure_cost_base)} />
        <KpiTile size="sm" testId="departure-revenue" label={tr("departure.total.revenue")} value={money(d.totals.revenue_base)} />
        <KpiTile
          size="sm"
          testId="departure-margin"
          label={tr("departure.total.margin")}
          value={money(d.totals.margin_base)}
          valueTone={margin < 0 ? "danger" : margin > 0 ? "success" : undefined}
        />
      </div>
      <Panel title={tr("departure.costs")} testId="departure-costs">
        <DepartureCosts
          departureRef={d.departure_ref}
          costs={d.costs}
          suppliers={suppliers.ok ? suppliers.data.suppliers.filter((s) => s.kind === "own") : []}
          lang={ctx.lang}
          locale={locale}
        />
        {d.costs_other_base > 0 && <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{tr("departure.otherBase")}</p>}
      </Panel>
      <Panel title={tr("departure.trips")} testId="departure-trips">
        {d.trips.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("departure.noTrips")}</p>
        ) : (
          <ReportTable
            tableId="departure-trips"
            head={[tr("departure.col.trip"), tr("departure.col.pax"), tr("departure.col.share"), tr("departure.col.revenue"), tr("departure.col.cost"), tr("departure.col.margin")]}
            rows={d.trips.map((x) => ({
              key: x.trip_id,
              href: `/pipeline/${x.trip_id}`,
              cells: [
                x.start_date ? `${x.title} · ${formatDate(x.start_date, locale)}` : x.title,
                String(x.pax),
                money(x.departure_cost_base),
                money(x.revenue_base),
                money(x.total_cost_base),
                money(x.margin_base),
              ],
            }))}
            empty={tr("departure.noTrips")}
          />
        )}
      </Panel>
    </div>
  );
}
