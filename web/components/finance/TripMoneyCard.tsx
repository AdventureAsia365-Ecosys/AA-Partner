// web/components/finance/TripMoneyCard.tsx — AA-1068 (crm-30, crm-25, crm-32): the trip's Money card. The profit of the
// trip in the agency's base currency (GET /trips/{id}/pnl: revenue from issued invoices net of tax and refunds, costs from
// AA, the agency's suppliers (the bill once billed), AA cancellation fees and the group departure share, the supplier
// commission), what is not counted yet, the trip's group code and departures, and its supplier bills. Owner and agent
// only: the trip page renders it for them, and the API refuses the assistant.
import Link from "next/link";
import { K, KpiTile } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { GroupCodeForm } from "@/components/finance/GroupCodeForm";
import { SupplierBills } from "@/components/finance/SupplierBills";
import type { DateLocale } from "@/lib/dates";
import { departureHref, type SupplierBill, type TripPnl } from "@/lib/finance";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney, type Line } from "@/lib/ledger";

function Row({ label, value, hint, strong }: { label: string; value: string; hint?: string | null; strong?: boolean }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13, fontWeight: strong ? 600 : 400 }}>
      <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
        {label}
        {hint && <span style={{ color: K.muted, fontWeight: 400 }}> · {hint}</span>}
      </span>
      <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{value}</span>
    </div>
  );
}

export function TripMoneyCard({
  tripId,
  pnl,
  bills,
  lines,
  lang,
  locale,
  closed,
}: {
  tripId: string;
  pnl: TripPnl;
  bills: SupplierBill[];
  lines: Line[];
  lang: Lang;
  locale: DateLocale;
  closed: boolean;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const cur = pnl.base_currency;
  const m = (v: string | null | undefined) => formatMoney(v, cur, locale);
  const margin = Number(pnl.margin_base);
  const billable = lines.filter((l) => l.supplier_kind === "agency" && l.status === "confirmed");
  return (
    <Panel title={tr("finance.money.title")} testId="trip-money">
      <p style={{ margin: "0 0 12px", fontSize: 12.5, color: K.muted }}>{fmt(tr("finance.money.subtitle"), { currency: cur })}</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        <KpiTile size="sm" testId="money-revenue" label={tr("finance.revenue")} value={m(pnl.revenue.total_base)} />
        <KpiTile size="sm" testId="money-costs" label={tr("finance.costs")} value={m(pnl.costs.total_base)} />
        <KpiTile
          size="sm"
          testId="money-profit"
          label={tr("finance.margin")}
          value={m(pnl.margin_base)}
          valueTone={margin < 0 ? "danger" : margin > 0 ? "success" : undefined}
          hint={pnl.margin_pct !== null ? `${tr("finance.marginPct")} ${Number(pnl.margin_pct).toLocaleString(locale)} %` : undefined}
        />
        <KpiTile size="sm" testId="money-per-pax" label={tr("finance.perPax")} value={m(pnl.margin_per_pax_base)} hint={`× ${pnl.pax}`} />
      </div>
      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", marginTop: 14 }}>
        <div style={{ display: "grid", gap: 6 }} data-testid="money-revenue-rows">
          <Row label={tr("finance.revenue.sales")} value={m(pnl.revenue.sales_base)} />
          <Row label={tr("finance.revenue.serviceFees")} value={m(pnl.revenue.service_fees_base)} />
          <Row label={tr("finance.revenue.cancellationFees")} value={m(pnl.revenue.cancellation_fees_base)} />
          <Row label={tr("finance.revenue.refunds")} value={`− ${m(pnl.revenue.refunds_base)}`} />
          <Row label={tr("finance.commissionIncome")} value={m(pnl.commission_income_base)} />
          <Row label={tr("finance.revenue")} value={m(pnl.revenue.total_base)} strong />
        </div>
        <div style={{ display: "grid", gap: 6 }} data-testid="money-cost-rows">
          <Row
            label={tr("finance.cost.aa")}
            value={m(pnl.costs.aa_base)}
            hint={pnl.costs.aa_base !== pnl.costs.aa_quoted_base ? fmt(tr("finance.cost.aaQuoted"), { amount: m(pnl.costs.aa_quoted_base) }) : null}
          />
          <Row
            label={tr("finance.cost.agency")}
            value={m(pnl.costs.agency_base)}
            hint={pnl.costs.variance_base !== null ? fmt(tr("finance.cost.agencyPlanned"), { amount: m(pnl.costs.agency_planned_base) }) : null}
          />
          {pnl.costs.variance_base !== null && <Row label={tr("finance.cost.variance")} value={m(pnl.costs.variance_base)} />}
          <Row label={tr("finance.cost.cancellation")} value={m(pnl.costs.cancellation_base)} />
          <Row label={tr("finance.cost.departure")} value={m(pnl.costs.departure_base)} />
          <Row label={tr("finance.cost.partner")} value={m(pnl.costs.partner_commission_base)} />
          <Row label={tr("finance.costs")} value={m(pnl.costs.total_base)} strong />
        </div>
      </div>
      {pnl.notes.length > 0 && (
        <ul style={{ margin: "12px 0 0", paddingLeft: 18, fontSize: 12.5, color: K.muted }} data-testid="money-notes">
          {pnl.notes.map((n) => (
            <li key={n}>
              {n === "aa_invoice_mismatch" ? <Link href="/aa-bookings">{tr(`finance.note.${n}`)}</Link> : tr(`finance.note.${n}`)}
            </li>
          ))}
        </ul>
      )}
      <div style={{ display: "grid", gap: 8, marginTop: 16 }} data-testid="money-group">
        <h3 style={{ margin: 0, fontSize: 13 }}>{tr("finance.group.title")}</h3>
        <GroupCodeForm tripId={tripId} code={pnl.group_code} lang={lang} disabled={closed} />
        <p style={{ margin: 0, fontSize: 12.5 }}>
          {pnl.departure_refs.length === 0 ? (
            <span style={{ color: K.muted }}>{tr("finance.group.none")}</span>
          ) : (
            <>
              <span style={{ color: K.muted }}>{tr("finance.group.departures")}: </span>
              {pnl.departure_refs.map((ref, i) => (
                <span key={ref}>
                  {i > 0 && ", "}
                  <Link href={departureHref(ref)}>{ref}</Link>
                </span>
              ))}
            </>
          )}
        </p>
      </div>
      <div style={{ marginTop: 16 }}>
        <SupplierBills bills={bills} lines={billable} lang={lang} locale={locale} closed={closed} />
      </div>
    </Panel>
  );
}
