// web/app/(crm)/settings/data/page.tsx — A-18 Settings > Data and privacy (AA-942, card CRM-14; design v3 §3.5, §3.6,
// §4.10): the retention rules per data class, the agency's trash window and archive setting, legal holds, the trash,
// clients archived after inactivity, the last lifecycle runs, and leaving AA Partner (export, then erase). Every role
// sees the rules; the owner sees and changes the rest (the API decides; other roles get the rules only).
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { DataLifecycleView } from "@/components/settings/DataLifecycleView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { LifecycleView, TrashItem } from "@/lib/lifecycle";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function DataSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const [view, bin] = session
    ? await Promise.all([
        crmCall<LifecycleView>(ctx.cfg, session, "GET", "lifecycle"),
        crmCall<{ items: TrashItem[]; trash_days: number }>(ctx.cfg, session, "GET", "trash"),
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
    </div>
  );
}
