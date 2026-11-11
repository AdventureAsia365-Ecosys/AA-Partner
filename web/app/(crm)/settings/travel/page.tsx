// web/app/(crm)/settings/travel/page.tsx — Settings > Travel (AA-1070, crm-054 / crm-055): the passport rule (months a
// passport must outlast the trip) and the agency's own visa guidance per country and visa type. Every role reads it;
// only the owner changes it (the API refuses the others).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { TravelSettingsView } from "@/components/travelOps/TravelSettingsView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import type { VisaTemplate } from "@/lib/travelOps";

export const dynamic = "force-dynamic";

export default async function TravelSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const [settings, templates] = await Promise.all([
    crmCall<{ passport_valid_months: number }>(ctx.cfg, session, "GET", "settings/travel"),
    crmCall<{ templates: VisaTemplate[] }>(ctx.cfg, session, "GET", "visa-templates"),
  ]);
  const isOwner = ctx.role === "owner";
  return (
    <div data-testid="crm-travel-settings" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "travel.settings.title")} subtitle={t(ctx.lang, isOwner ? "travel.settings.subtitle" : "travel.settings.ownerOnly")} />
      {settings.ok && templates.ok ? (
        <TravelSettingsView months={settings.data.passport_valid_months} templates={templates.data.templates} isOwner={isOwner} lang={ctx.lang} />
      ) : (
        <ErrorState message={t(ctx.lang, "error.api")} />
      )}
    </div>
  );
}
