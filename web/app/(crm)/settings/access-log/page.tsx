// web/app/(crm)/settings/access-log/page.tsx — A-18 Settings > Data & privacy > Access log (AA-940, card CRM-12;
// design v3 §4.9 "agency page: 12 months in the app, plus download"; crm-design-final WF-8 and §3.6 "Access log:
// owner yes, agent trip Activity only, assistant no"). Owner only: other roles see why, and the page does not call
// the API for them (the API refuses too and logs the refusal). The first page is a server render; filters live in
// the URL; older entries and the download go through the BFF (lib/bff.ts).
import { EmptyState, ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { AccessLogView } from "@/components/settings/AccessLogView";
import { type AuditPage, cleanFilter, filterQuery } from "@/lib/accessLog";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function AccessLogPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const filter = cleanFilter(await searchParams);
  const back = (
    <a href="/settings" style={{ fontSize: 13 }}>
      ← {t(ctx.lang, "settings.title")}
    </a>
  );
  if (ctx.role !== "owner") {
    return (
      <div data-testid="crm-access-log" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
        {back}
        <PageTitle title={t(ctx.lang, "log.title")} />
        <EmptyState title={t(ctx.lang, "log.ownerOnly")} />
      </div>
    );
  }
  const { session } = await serverContext();
  const res = session
    ? await crmCall<AuditPage>(ctx.cfg, session, "GET", "audit/events", { query: filterQuery(filter, { limit: "50" }) })
    : null;
  return (
    <div data-testid="crm-access-log" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
      {back}
      <PageTitle title={t(ctx.lang, "log.title")} subtitle={t(ctx.lang, "log.subtitle")} />
      {!res || !res.ok ? (
        <ErrorState message={t(ctx.lang, "log.error")} />
      ) : (
        <AccessLogView
          key={filterQuery(filter).toString()}
          initial={res.data}
          filter={filter}
          lang={ctx.lang}
          locale={ctx.locale}
        />
      )}
    </div>
  );
}
