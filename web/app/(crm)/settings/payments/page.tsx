// web/app/(crm)/settings/payments/page.tsx — Settings > Payments, the CRM-16 part (AA-944): base currency, booking
// validation level, exchange rates automatic / manual and the owner's own rates; CRM-23 slice 2: the invoice settings
// (prefix, tax, due days, service fees); slice 3: how clients pay the agency (its manual method); AA-1030: the retail markup
// on AA products the builder suggests a sell price from; AA-1067: the payment schedule template; AA-1072: the agency's
// own online gateway (VNPay) and the "Book now" switch. Owner only: other roles see why without calling the API.
import Link from "next/link";
import { ErrorState, K } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { InvoiceSettingsForm } from "@/components/invoices/InvoiceSettingsForm";
import { PaymentMethodsForm } from "@/components/invoices/PaymentMethodsForm";
import { PaymentsView } from "@/components/ledger/PaymentsView";
import { RetailMarkupForm, type RetailMarkupView } from "@/components/proposals/RetailMarkupForm";
import { ScheduleTemplateForm } from "@/components/settings/ScheduleTemplateForm";
import { OnlineBookingForm, type OnlineBookingView } from "@/components/settings/OnlineBookingForm";
import { OnlinePaymentForm, type GatewayStatus } from "@/components/settings/OnlinePaymentForm";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { InvoiceSettings, PaymentMethodsView } from "@/lib/invoices";
import type { FxOverride, PaymentSettings } from "@/lib/ledger";
import type { ScheduleSettings } from "@/lib/retailPayments";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PaymentsSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const back = (
    <Link href="/settings" style={{ fontSize: 13 }}>
      ← {t(ctx.lang, "settings.title")}
    </Link>
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
  const [settings, overrides, invoiceSettings, methods, markup, schedule, gateway, booking] = await Promise.all([
    crmCall<PaymentSettings>(ctx.cfg, session, "GET", "settings/payments"),
    crmCall<{ overrides: FxOverride[] }>(ctx.cfg, session, "GET", "fx/overrides"),
    crmCall<InvoiceSettings>(ctx.cfg, session, "GET", "settings/invoices"),
    crmCall<PaymentMethodsView>(ctx.cfg, session, "GET", "settings/payment-methods"),
    crmCall<RetailMarkupView>(ctx.cfg, session, "GET", "settings/retail-markup"),
    crmCall<ScheduleSettings>(ctx.cfg, session, "GET", "settings/payment-schedule"),
    crmCall<GatewayStatus>(ctx.cfg, session, "GET", "settings/payment-methods/vn-gateway"),
    crmCall<OnlineBookingView>(ctx.cfg, session, "GET", "settings/online-booking"),
  ]);
  const stubAllowed = ["dev", "test", "local"].includes(ctx.cfg.environment);
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
      {methods.ok && <PaymentMethodsForm view={methods.data} lang={ctx.lang} />}
      {gateway.ok && <OnlinePaymentForm status={gateway.data} lang={ctx.lang} stubAllowed={stubAllowed} />}
      {booking.ok && <OnlineBookingForm view={booking.data} lang={ctx.lang} />}
      {invoiceSettings.ok && <InvoiceSettingsForm settings={invoiceSettings.data} lang={ctx.lang} />}
      {markup.ok && <RetailMarkupForm view={markup.data} lang={ctx.lang} />}
      {schedule.ok && <ScheduleTemplateForm view={schedule.data} lang={ctx.lang} />}
    </div>
  );
}
