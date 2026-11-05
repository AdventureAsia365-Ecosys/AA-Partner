// web/app/(crm)/bookings/page.tsx — A-11 Bookings (AA-944, card CRM-16; design v3 M6, R18): every booking line across
// the agency's trips with a status filter. Only confirmed lines count in the totals (base currency, frozen amounts);
// planning sell shows as pipeline value. Totals, cost and margin come only for owner and agent (the API decides).
import Link from "next/link";
import { ErrorState, K } from "@/app/_kit";
import { BookingLinesTable } from "@/components/ledger/BookingLinesTable";
import { Panel, PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { formatMoney, type LedgerView, type LineStatus } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const STATUSES: readonly LineStatus[] = ["planning", "confirmed", "cancelled"];

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
    <div data-testid="crm-bookings" style={{ display: "grid", gap: 14, maxWidth: 1200 }}>
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
            {totals.cancellation_cost_base !== undefined && totals.cancellation_cost_base !== "0" && totals.cancellation_cost_base !== "0.00" && (
              <>
                <dt style={{ color: K.muted }}>{tr("ledger.totals.cancellationCost")}</dt>
                <dd style={{ margin: 0 }} data-testid="bookings-cancellation-cost">
                  {money(totals.cancellation_cost_base)}
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
        <Link href="/bookings" aria-current={status === null ? "page" : undefined}>
          {tr("ledger.filter.all")}
        </Link>
        {STATUSES.map((s) => (
          <a key={s} href={`/bookings?status=${s}`} aria-current={status === s ? "page" : undefined}>
            {tr(`ledger.status.${s}`)}
          </a>
        ))}
      </nav>
      {lines.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13 }}>{tr("ledger.empty")}</p>
      ) : (
        <BookingLinesTable lines={lines} lang={ctx.lang} locale={ctx.locale} />
      )}
      {view.data.truncated && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("ledger.truncated")}</p>}
      <a href="https://www.exchangerate-api.com" target="_blank" rel="noreferrer" style={{ fontSize: 11.5 }}>
        Rates By Exchange Rate API
      </a>
    </div>
  );
}
