// web/app/(crm)/suppliers/page.tsx — A-13 Suppliers (AA-944, card CRM-16; design v3 M15): Adventure Asia built in and
// pinned, the agency's own suppliers with their contact behind a reveal. One list_view audit event per load (API).
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { SuppliersView } from "@/components/ledger/SuppliersView";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { SuppliersView as View } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function SuppliersPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const view = await crmCall<View>(ctx.cfg, session, "GET", "suppliers");
  return (
    <div data-testid="crm-suppliers" data-card="AA-944" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
      <PageTitle title={t(ctx.lang, "suppliers.title")} subtitle={t(ctx.lang, "suppliers.subtitle")} />
      {!view.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <SuppliersView suppliers={view.data.suppliers} categories={view.data.categories} role={ctx.role} lang={ctx.lang} />
      )}
    </div>
  );
}
