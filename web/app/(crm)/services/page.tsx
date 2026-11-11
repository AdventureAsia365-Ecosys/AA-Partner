// web/app/(crm)/services/page.tsx — AA-1069 (crm-052): the agency's own service catalogue (flights, insurance, visas,
// transfers, fees) with prices per unit and how each is counted, ready for the builder's "From catalogue" tab. Every
// role reads it (the supplier cost and the group prices only for owner and agent); owner and agent edit it.
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { ServicesView } from "@/components/catalogue/ServicesView";
import type { CatalogueView } from "@/lib/catalogue";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { SuppliersView } from "@/lib/ledger";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ServicesPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const [view, suppliers] = await Promise.all([
    crmCall<CatalogueView>(ctx.cfg, session, "GET", "services"),
    crmCall<SuppliersView>(ctx.cfg, session, "GET", "suppliers"),
  ]);
  const own = suppliers.ok ? suppliers.data.suppliers.filter((s) => s.kind === "own").map((s) => ({ agency_supplier_id: s.agency_supplier_id, name: s.name })) : [];
  return (
    <div data-testid="crm-services" style={{ display: "grid", gap: 14, maxWidth: 1080, minWidth: 0 }}>
      <PageTitle title={t(ctx.lang, "services.title")} subtitle={t(ctx.lang, "services.subtitle")} />
      {!view.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <ServicesView
          services={view.data.services}
          categories={view.data.categories}
          bases={view.data.qty_bases}
          suppliers={own}
          role={ctx.role}
          lang={ctx.lang}
          locale={ctx.locale}
        />
      )}
    </div>
  );
}
