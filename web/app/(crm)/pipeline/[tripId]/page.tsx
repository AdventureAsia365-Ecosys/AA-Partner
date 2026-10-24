// web/app/(crm)/pipeline/[tripId]/page.tsx — A-03 trip workspace, Slice 1 (AA-936): header with stage, waiting on and
// owner; the party (names; e-mail / phone only on a click, each click audited); tasks; notes; the inquiry answers
// (no identity values) and the activity. One "view" audit event per load (API). AA-937: the trip's documents
// (one list_view of the file names), companions and dates. AA-943 (CRM-15): the Traveller info panel reads
// GET /trips/{id}/stage2 (completeness with the missing codes, the planning form's pages, the F2 answers); the
// planning form's link opens on the first client host.
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { TripPanel } from "@/components/pipeline/TripPanel";
import type { DocumentRow } from "@/lib/clients";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { Member, Stage2View, TripView } from "@/lib/pipeline";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function TripPage({ params }: { params: Promise<{ tripId: string }> }) {
  const { tripId } = await params;
  if (!UUID.test(tripId)) notFound();
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const [trip, members, docs, stage2] = await Promise.all([
    crmCall<TripView>(ctx.cfg, session, "GET", `trips/${tripId}`),
    crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members"),
    crmCall<{ documents: DocumentRow[] }>(ctx.cfg, session, "GET", `trips/${tripId}/documents`),
    crmCall<Stage2View>(ctx.cfg, session, "GET", `trips/${tripId}/stage2`),
  ]);
  const clientHost = ctx.cfg.clientHosts[0];
  if (!trip.ok) {
    if (trip.status === 404) {
      return <ErrorState message={t(ctx.lang, "trip.notFound")} />;
    }
    return <ErrorState message={t(ctx.lang, "error.api")} />;
  }
  return (
    <TripPanel
      view={trip.data}
      members={members.ok ? members.data.members : []}
      role={ctx.role}
      lang={ctx.lang}
      locale={ctx.locale}
      documents={docs.ok ? docs.data.documents : []}
      stage2={stage2.ok ? stage2.data : null}
      clientOrigin={clientHost ? `https://${clientHost}` : null}
    />
  );
}
