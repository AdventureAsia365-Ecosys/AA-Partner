// web/app/(crm)/settings/pricing/page.tsx — Settings > Pricing (AA-1066, crm-048): the age bands of traveller prices,
// the discount staff may give without the owner's approval, and the retail price rules the builder applies to a new
// AA product's suggested sell. Owner only: other roles see why without calling the API.
import Link from "next/link";
import { ErrorState, K } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { PricingSettingsForm } from "@/components/proposals/PricingSettingsForm";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { PricingSettings } from "@/lib/retailPricing";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PricingSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const back = (
    <Link href="/settings" style={{ fontSize: 13 }}>
      ← {t(ctx.lang, "settings.title")}
    </Link>
  );
  const title = <PageTitle title={t(ctx.lang, "pricingSettings.title")} subtitle={t(ctx.lang, "pricingSettings.subtitle")} />;
  if (ctx.role !== "owner") {
    return (
      <div data-testid="crm-pricing-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
        {back}
        {title}
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="pricing-owner-only">
          {t(ctx.lang, "pricingSettings.ownerOnly")}
        </p>
      </div>
    );
  }
  const { session } = await serverContext();
  if (!session) return null;
  const view = await crmCall<PricingSettings>(ctx.cfg, session, "GET", "settings/pricing");
  return (
    <div data-testid="crm-pricing-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      {back}
      {title}
      {view.ok ? <PricingSettingsForm view={view.data} lang={ctx.lang} locale={ctx.locale} /> : <ErrorState message={t(ctx.lang, "error.api")} />}
    </div>
  );
}
