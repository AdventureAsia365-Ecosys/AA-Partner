// web/components/finance/ProfitSection.tsx — AA-1068 (crm-31): the Reports page's profit cube (GET /reports/profit):
// the booked trips that travel in the period, grouped by lead source, AA tour or the agency's own services, travel
// month, owner or sales partner, with revenue (invoices net of tax and refunds), costs, supplier commission, profit and
// margin, and the CSV download of the same rows. Owner and agent only (the page reads it only for a role that sees
// money, and the API refuses the assistant). The page passes the answer; a failed read shows nothing.
import { K } from "@/app/_kit";
import { ProfitExport } from "@/components/finance/ProfitExport";
import { Panel } from "@/components/PageTitle";
import { ReportTable } from "@/components/reports/ReportTable";
import type { DateLocale } from "@/lib/dates";
import { DIMENSIONS, type Dimension, type ProfitRow, type ProfitView } from "@/lib/finance";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";
import { leadSourceLabel } from "@/lib/clients";
import type { Member } from "@/lib/pipeline";
import { memberLabel } from "@/lib/pipeline";
import { reportQuery, type Period } from "@/lib/reports";

export function ProfitSection({
  view: v,
  period,
  owner,
  dimension,
  members,
  lang,
  locale,
}: {
  view: ProfitView;
  period: Period;
  owner: string | null;
  dimension: Dimension;
  members: Member[];
  lang: Lang;
  locale: DateLocale;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const money = (x: string) => formatMoney(x, v.base_currency, locale);
  const label = (r: ProfitRow): string => {
    if (r.key === "total") return tr("profit.total");
    if (["aa_tour", "ancillary", "none", "unknown", "unassigned"].includes(r.key)) return tr(`profit.key.${r.key}`);
    if (dimension === "lead_source") return leadSourceLabel(lang, r.key);
    if (dimension === "owner_member") {
      const m = members.find((x) => x.member_id === r.key);
      return m ? memberLabel(lang, m) : tr("reports.formerMember");
    }
    if (dimension === "travel_month") {
      return new Date(`${r.key}-01T00:00:00Z`).toLocaleDateString(locale, { month: "short", year: "numeric", timeZone: "UTC" });
    }
    return r.key;
  };
  const cells = (r: ProfitRow) => [
    label(r),
    String(r.trips),
    String(r.pax),
    money(r.revenue_base),
    money(r.cost_base),
    money(r.commission_income_base),
    money(r.margin_base),
    r.margin_pct === null ? "—" : `${Number(r.margin_pct).toLocaleString(locale)} %`,
  ];
  const href = (d: Dimension) => `/reports?${reportQuery({ period, owner }, { dimension: d }).toString()}#profit`;
  return (
    <Panel title={tr("profit.title")} testId="reports-profit">
      <div id="profit" style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <nav aria-label={tr("profit.by")} style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 13 }}>
          <span style={{ color: K.muted }}>{tr("profit.by")}:</span>
          {DIMENSIONS.map((d) => (
            <a key={d} href={href(d)} aria-current={d === dimension ? "page" : undefined} style={{ fontWeight: d === dimension ? 600 : 400 }}>
              {tr(`profit.dim.${d}`)}
            </a>
          ))}
        </nav>
        <ProfitExport lang={lang} period={period} owner={owner} dimension={dimension} />
      </div>
      <p style={{ margin: "0 0 8px", fontSize: 12.5, color: K.muted }}>{tr("profit.hint")}</p>
      <ReportTable
        tableId="profit"
        head={[
          tr(`profit.dim.${dimension}`),
          tr("profit.col.trips"),
          tr("profit.col.pax"),
          tr("profit.col.revenue"),
          tr("profit.col.cost"),
          tr("profit.col.income"),
          tr("profit.col.margin"),
          tr("profit.col.marginPct"),
        ]}
        rows={[...v.rows, ...(v.rows.length ? [v.totals] : [])].map((r) => ({ key: r.key, cells: cells(r) }))}
        empty={tr("profit.none")}
      />
      {v.trips_unconverted > 0 && (
        <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{fmt(tr("profit.unconverted"), { n: v.trips_unconverted })}</p>
      )}
    </Panel>
  );
}
