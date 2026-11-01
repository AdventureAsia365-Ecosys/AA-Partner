// web/app/(crm)/pipeline/[tripId]/proposal/[proposalId]/page.tsx — A-04, the day-list builder of one proposal version
// (AA-949, card CRM-21a). Reads GET /proposals/{id}/builder (one "view" audit event; net and margin only for owner /
// agent), every booking terms version (the select shows the published one, and a retired one the version still
// names: S262 round 3) and the agency's suppliers (the drawer's agency items). The page is a client component from
// there on (components/proposals/BuilderPage.tsx); every write goes through the BFF lines of card AA-949 in lib/bff.ts.
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { BuilderPage } from "@/components/proposals/BuilderPage";
import type { BuilderView } from "@/lib/builder";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { SuppliersView } from "@/lib/ledger";
import type { Agreement } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function BuilderRoute({ params }: { params: Promise<{ tripId: string; proposalId: string }> }) {
  const { tripId, proposalId } = await params;
  if (!UUID.test(tripId) || !UUID.test(proposalId)) notFound();
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const [view, agreements, suppliers] = await Promise.all([
    crmCall<BuilderView>(ctx.cfg, session, "GET", `proposals/${proposalId}/builder`),
    crmCall<{ agreements: Agreement[] }>(ctx.cfg, session, "GET", "agreements"),
    crmCall<SuppliersView>(ctx.cfg, session, "GET", "suppliers"),
  ]);
  if (!view.ok) {
    if (view.status === 404) return <ErrorState message={t(ctx.lang, "builder.notFound")} />;
    if (view.status === 409) return <ErrorState message={t(ctx.lang, "builder.notBuilder")} />;
    return <ErrorState message={t(ctx.lang, "error.api")} />;
  }
  if (view.data.trip_id !== tripId) notFound();
  return (
    <BuilderPage
      initial={view.data}
      role={ctx.role}
      lang={ctx.lang}
      locale={ctx.locale}
      terms={agreements.ok ? agreements.data.agreements.filter((d) => d.kind === "booking_terms") : []}
      suppliers={suppliers.ok ? suppliers.data.suppliers.map((s) => ({ agency_supplier_id: s.agency_supplier_id, name: s.name, kind: s.kind })) : []}
    />
  );
}
