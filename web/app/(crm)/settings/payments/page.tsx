// web/app/(crm)/settings/payments/page.tsx — Settings > Payments, the CRM-16 part (AA-944): base currency, booking
// validation level, exchange rates automatic / manual and the owner's own rates; CRM-23 slice 2: the invoice settings
// (prefix, tax, due days, service fees). Owner only: other roles see why without calling the API.
import { ErrorState, K } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { InvoiceSettingsForm } from "@/components/invoices/InvoiceSettingsForm";
import { PaymentsView } from "@/components/ledger/PaymentsView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { InvoiceSettings } from "@/lib/invoices";
import type { FxOverride, PaymentSettings } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PaymentsSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const back = (
    <a href="/settings" style={{ fontSize: 13 }}>
      ← {t(ctx.lang, "settings.title")}
    </a>
  );
  if (ctx.role !== "owner") {
    return (
      <div data-testid="crm-payments-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
        {back}
        <PageTitle title={t(ctx.lang, "payments.title")} subtitle={t(ctx.lang, "payments.subtitle")} />
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="payments-owner-only">
          {t(ctx.lang, "payments.ownerOnly")}
        </p>
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const [settings, overrides, invoiceSettings] = await Promise.all([
    crmCall<PaymentSettings>(ctx.cfg, session, "GET", "settings/payments"),
    crmCall<{ overrides: FxOverride[] }>(ctx.cfg, session, "GET", "fx/overrides"),
    crmCall<InvoiceSettings>(ctx.cfg, session, "GET", "settings/invoices"),
  ]);
  return (
    <div data-testid="crm-payments-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      {back}
      <PageTitle title={t(ctx.lang, "payments.title")} subtitle={t(ctx.lang, "payments.subtitle")} />
      {!settings.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <PaymentsView
          settings={settings.data}
          overrides={overrides.ok ? overrides.data.overrides : []}
          lang={ctx.lang}
          locale={ctx.locale}
        />
      )}
      {invoiceSettings.ok && <InvoiceSettingsForm settings={invoiceSettings.data} lang={ctx.lang} />}
    </div>
  );
}
