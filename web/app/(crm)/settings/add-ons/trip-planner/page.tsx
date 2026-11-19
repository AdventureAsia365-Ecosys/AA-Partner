// web/app/(crm)/settings/add-ons/trip-planner/page.tsx — Settings > Add-ons > Trip planner (AA-1139): the agency's
// TripPlanner module. The CRM API relays to CIS (the planner's address, domains, lead route, theme, locale) and to
// TripPlanner (leads, usage, the webhook signing key). The owner and agents see it (the API refuses the assistant and
// asks no one); only the owner changes it (the view is read-only for an agent, and the API refuses anyway).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { TripPlannerModuleView } from "@/components/settings/TripPlannerModuleView";
import { CRM_ADD_ONS_PATH } from "@/lib/addOns";
import { type ApiResult, crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { type DateLocale } from "@/lib/dates";
import { type Lang, t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import { loadErrorKey, type PlannerLeadList, type PlannerPage } from "@/lib/tripPlannerModule";

export const dynamic = "force-dynamic";

function Body({ page, leads, owner, lang, locale }: {
  page: ApiResult<PlannerPage> | null;
  leads: ApiResult<PlannerLeadList> | null;
  owner: boolean;
  lang: Lang;
  locale: DateLocale;
}) {
  if (!page) {
    return (
      <p data-testid="tp-no-access" style={{ margin: 0, fontSize: 13 }}>
        {t(lang, "tp.err.noAccess")}
      </p>
    );
  }
  if (!page.ok) return <ErrorState message={t(lang, loadErrorKey(page.status, page.detail))} />;
  return <TripPlannerModuleView page={page.data} leads={leads?.ok ? leads.data : null} owner={owner} lang={lang} locale={locale} />;
}

export default async function TripPlannerPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const canSee = ctx.role === "owner" || ctx.role === "agent";
  const page = canSee ? await crmCall<PlannerPage>(ctx.cfg, session, "GET", "trip-planner") : null;
  const active = page?.ok === true && page.data.module.active;
  const leads = active ? await crmCall<PlannerLeadList>(ctx.cfg, session, "GET", "trip-planner/leads") : null;
  return (
    <div data-testid="crm-trip-planner" style={{ display: "grid", gap: 14, maxWidth: 960 }}>
      <Link href={CRM_ADD_ONS_PATH} style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "addons.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "tp.title")} subtitle={t(ctx.lang, "tp.subtitle")} />
      <Body page={page} leads={leads} owner={ctx.role === "owner"} lang={ctx.lang} locale={ctx.locale} />
    </div>
  );
}
