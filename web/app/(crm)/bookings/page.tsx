// web/app/(crm)/bookings/page.tsx — A-11 Bookings (AA-944, card CRM-16; design v3 M6, R18): every booking line across
// the agency's trips with a status filter. Only confirmed lines count in the totals (base currency, frozen amounts);
// planning sell shows as pipeline value. Totals, cost and margin come only for owner and agent (the API decides).
import { Badge, ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatMoney, fxReading, type LedgerView, type LineStatus } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const STATUSES: readonly LineStatus[] = ["planning", "confirmed", "cancelled"];
const TONE = { planning: "neutral", confirmed: "success", cancelled: "danger" } as const;
const cell: React.CSSProperties = { padding: "7px 8px", borderTop: `1px solid ${K.line}`, verticalAlign: "top", fontSize: 13 };
const head: React.CSSProperties = { ...cell, borderTop: "none", color: K.muted, fontSize: 11.5, fontWeight: 600, textAlign: "left" };

export default async function BookingsPage({ searchParams }: { searchParams: Promise<{ status?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const { status: raw } = await searchParams;
  const status = STATUSES.includes(raw as LineStatus) ? (raw as LineStatus) : null;
  const query = status ? new URLSearchParams({ status }) : undefined;
  const view = await crmCall<LedgerView>(ctx.cfg, session, "GET", "bookings", { query });
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!view.ok) return <ErrorState message={tr("error.api")} />;
  const { lines, totals, base_currency: base } = view.data;
  const money = (v: string | undefined) => formatMoney(v, base, ctx.locale);
  return (
    <div data-testid="crm-bookings" data-card="AA-944" style={{ display: "grid", gap: 14, maxWidth: 1200 }}>
      <PageTitle title={tr("ledger.page.title")} subtitle={tr("ledger.page.subtitle")} />
      {totals && (
        <Panel title={tr("ledger.totals")} testId="bookings-totals">
          <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 16px", margin: 0, fontSize: 13 }}>
            <dt style={{ color: K.muted }}>{tr("ledger.totals.confirmedSell")}</dt>
            <dd style={{ margin: 0 }}>{money(totals.confirmed_sell_base)}</dd>
            {totals.confirmed_cost_base !== undefined && (
              <>
                <dt style={{ color: K.muted }}>{tr("ledger.totals.confirmedCost")}</dt>
                <dd style={{ margin: 0 }}>{money(totals.confirmed_cost_base)}</dd>
              </>
            )}
            {totals.confirmed_margin_base !== undefined && (
              <>
                <dt style={{ color: K.muted }}>{tr("ledger.totals.margin")}</dt>
                <dd style={{ margin: 0 }} data-testid="bookings-margin">
                  {money(totals.confirmed_margin_base)}
                </dd>
              </>
            )}
            <dt style={{ color: K.muted }}>{tr("ledger.totals.pipeline")}</dt>
            <dd style={{ margin: 0 }}>
              {money(totals.pipeline_sell_base)}
              {totals.pipeline_unconverted > 0 && (
                <span style={{ color: K.muted }}> · {fmt(tr("ledger.totals.unconverted"), { n: totals.pipeline_unconverted })}</span>
              )}
            </dd>
          </dl>
          {(totals.confirmed_other_base ?? 0) > 0 && (
            <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }} data-testid="bookings-other-base">
              {fmt(tr("ledger.totals.otherBase"), { n: totals.confirmed_other_base ?? 0 })}
            </p>
          )}
        </Panel>
      )}
      <nav style={{ display: "flex", gap: 10, fontSize: 13 }} aria-label={tr("ledger.col.status")}>
        <a href="/bookings" aria-current={status === null ? "page" : undefined}>
          {tr("ledger.filter.all")}
        </a>
        {STATUSES.map((s) => (
          <a key={s} href={`/bookings?status=${s}`} aria-current={status === s ? "page" : undefined}>
            {tr(`ledger.status.${s}`)}
          </a>
        ))}
      </nav>
      {lines.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13 }}>{tr("ledger.empty")}</p>
      ) : (
        <div style={{ overflowX: "auto", background: K.card, border: `1px solid ${K.line}`, borderRadius: 10 }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 820 }} data-testid="bookings-lines">
            <thead>
              <tr>
                {["day", "trip", "item", "supplier", "conf", "status", "sell", "fx", "collect"].map((c) => (
                  <th key={c} style={head}>
                    {tr(`ledger.col.${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.booking_line_id}>
                  <td style={cell}>{line.service_from ? formatDate(line.service_from, ctx.locale) : "—"}</td>
                  <td style={cell}>
                    <a href={`/pipeline/${line.trip_id}`}>{line.trip_title}</a>
                  </td>
                  <td style={cell}>{line.title}</td>
                  <td style={cell}>{line.supplier_name}</td>
                  <td style={cell}>{line.confirmation_no ?? "—"}</td>
                  <td style={cell}>
                    <Badge tone={TONE[line.status]}>{tr(`ledger.status.${line.status}`)}</Badge>
                  </td>
                  <td style={cell}>
                    {line.money?.sell ? formatMoney(line.money.sell.amount, line.money.sell.currency, ctx.locale) : "—"}
                  </td>
                  <td style={cell}>{fxReading(line.money?.sell) ?? "—"}</td>
                  <td style={cell}>{tr(`ledger.collect.${line.collect}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {view.data.truncated && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("ledger.truncated")}</p>}
      <a href="https://www.exchangerate-api.com" target="_blank" rel="noreferrer" style={{ fontSize: 11.5 }}>
        Rates By Exchange Rate API
      </a>
    </div>
  );
}
