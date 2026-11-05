// web/app/(crm)/aa-tariff/page.tsx — AA-1030: AA prices, the agency's own AA tariff (AA's B2B price for this agency per
// tour, season and group size, with its tier or contract discount; GET /crm/v1/aa/tariff, live from AA). Owner and
// agent (/me permissions.aa_tariff_view; the API refuses the assistant too). Currency select, Download CSV (AA's file,
// through the BFF) and Print (app/globals.css print rules). Indicative: a quote is the binding price.
// S270: the currency select offers USD plus the currencies AA has an approved FX rate for today (GET
// /crm/v1/aa/tariff/currencies, read first: the API caches it); a ?currency= that is not offered shows USD with a
// role=status notice, and a 422 aa_tariff_invalid says "pick another currency", never "AA cannot be reached".
import { EmptyState, ErrorState, K } from "@/app/_kit";
import { AaTariffTable, AaTariffToolbar } from "@/components/aaTariff/AaTariffView";
import { PageTitle } from "@/components/PageTitle";
import {
  offeredCurrencies,
  pickCurrency,
  seesAaTariff,
  TARIFF_CURRENCIES_PATH,
  TARIFF_TIMEOUT_MS,
  tariffErrorKey,
  type TariffCurrencies,
  type TariffView,
} from "@/lib/aaTariff";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { fmt, t, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

function tierLabel(code: string): string {
  return code ? code.charAt(0).toUpperCase() + code.slice(1).replace(/_/g, " ") : "";
}

export default async function AaTariffPage({ searchParams }: { searchParams: Promise<{ currency?: string | string[] }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const tr = (key: string) => t(ctx.lang, key as MessageKey);
  if (!seesAaTariff(ctx.me)) {
    return (
      <div data-testid="crm-section-forbidden">
        <PageTitle title={tr("tariff.title")} />
        <EmptyState title={tr("section.forbidden.title")} description={tr("section.forbidden.body")} />
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const offered = offeredCurrencies(await crmCall<TariffCurrencies>(ctx.cfg, session, "GET", TARIFF_CURRENCIES_PATH));
  const { currency, fellBack } = pickCurrency((await searchParams).currency, offered);
  const view = await crmCall<TariffView>(ctx.cfg, session, "GET", "aa/tariff", {
    query: new URLSearchParams({ currency }),
    timeoutMs: TARIFF_TIMEOUT_MS,
  });
  return (
    // minmax(0, 1fr): the table's min width never stretches the page's grid (S254 lesson).
    <div data-testid="crm-aa-tariff" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12, maxWidth: 1200 }}>
      <PageTitle title={tr("tariff.title")} subtitle={tr("tariff.subtitle")} />
      <p data-testid="tariff-indicative" style={{ margin: 0, fontSize: 13, color: K.ink, fontWeight: 600 }}>
        {tr("tariff.indicative")}
      </p>
      <AaTariffToolbar currency={currency} currencies={offered} lang={ctx.lang} />
      {fellBack && (
        <p role="status" style={{ margin: 0, fontSize: 12.5, color: K.amberDeep }} data-testid="tariff-currency-fallback">
          {fmt(tr("tariff.currencyFallback"), { currency: fellBack })}
        </p>
      )}
      {!view.ok ? (
        <div data-testid="tariff-error" data-detail={view.detail}>
          <ErrorState message={tr(tariffErrorKey(view.detail))} />
        </div>
      ) : (
        <>
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="tariff-meta">
            {fmt(tr("tariff.pricedOn"), { date: formatDate(view.data.priced_on, ctx.locale) })}
            {view.data.tier_code ? ` · ${fmt(tr("tariff.tier"), { tier: tierLabel(view.data.tier_code) })}` : ""}
          </p>
          {view.data.truncated && (
            <p role="status" style={{ margin: 0, fontSize: 12.5, color: K.amberDeep }} data-testid="tariff-truncated">
              {tr("tariff.truncated")}
            </p>
          )}
          <AaTariffTable items={view.data.items} lang={ctx.lang} locale={ctx.locale} />
        </>
      )}
    </div>
  );
}
