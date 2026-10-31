// web/app/(crm)/settings/data/page.tsx — A-18 Settings > Data and privacy (AA-942, card CRM-14; design v3 §3.5, §3.6,
// §4.10): the retention rules per data class, the agency's trash window and archive setting, legal holds, the trash,
// clients archived after inactivity, the last lifecycle runs, and leaving AA Partner (export, then erase); AA-967: the
// portability exports and the data-request log (owner); AA-971: AA support access (every role sees it, the owner
// allows or ends it). Every role
// sees the rules; the owner sees and changes the rest (the API decides; other roles get the rules only).
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { DataLifecycleView } from "@/components/settings/DataLifecycleView";
import { DataPortabilityView } from "@/components/settings/DataPortabilityView";
import { SupportAccessView } from "@/components/settings/SupportAccessView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { LifecycleView, TrashItem } from "@/lib/lifecycle";
import type { ExportsView, RequestsView } from "@/lib/portability";
import { serverContext } from "@/lib/serverContext";
import type { SupportAccess } from "@/lib/supportAccess";

export const dynamic = "force-dynamic";

export default async function DataSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const [view, bin, support] = session
    ? await Promise.all([
        crmCall<LifecycleView>(ctx.cfg, session, "GET", "lifecycle"),
        crmCall<{ items: TrashItem[]; trash_days: number }>(ctx.cfg, session, "GET", "trash"),
        crmCall<SupportAccess>(ctx.cfg, session, "GET", "support-access"),
      ])
    : [null, null, null];
  // AA-967: the owner's exports and data-request log (other roles: the API answers 403, the panels stay hidden).
  const owner = !!view && view.ok && view.data.role === "owner";
  const [exportsView, requestsView] =
    session && owner
      ? await Promise.all([
          crmCall<ExportsView>(ctx.cfg, session, "GET", "data/exports"),
          crmCall<RequestsView>(ctx.cfg, session, "GET", "data/requests"),
        ])
      : [null, null];
  return (
    <div data-testid="crm-data-settings" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
      <a href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </a>
      <PageTitle title={t(ctx.lang, "data.title")} subtitle={t(ctx.lang, "data.subtitle")} />
      {!view || !view.ok ? (
        <ErrorState message={t(ctx.lang, "data.error")} />
      ) : (
        <DataLifecycleView
          view={view.data}
          trash={bin && bin.ok ? bin.data.items : []}
          tenantId={ctx.me.tenant_id}
          lang={ctx.lang}
          locale={ctx.locale}
        />
      )}
      {support && support.ok && <SupportAccessView view={support.data} lang={ctx.lang} locale={ctx.locale} />}
      {owner && (
        <DataPortabilityView
          exportsView={exportsView && exportsView.ok ? exportsView.data : null}
          requestsView={requestsView && requestsView.ok ? requestsView.data : null}
          lang={ctx.lang}
          locale={ctx.locale}
        />
      )}
    </div>
  );
}
