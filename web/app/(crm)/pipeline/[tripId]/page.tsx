// web/app/(crm)/pipeline/[tripId]/page.tsx — A-03 trip workspace, Slice 1 (AA-936): header with stage, waiting on and
// owner; the party (names; e-mail / phone only on a click, each click audited); tasks; notes; the inquiry answers
// (no identity values) and the activity. One "view" audit event per load (API).
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { TripPanel } from "@/components/pipeline/TripPanel";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { Member, TripView } from "@/lib/pipeline";
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
  const [trip, members] = await Promise.all([
    crmCall<TripView>(ctx.cfg, session, "GET", `trips/${tripId}`),
    crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members"),
  ]);
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
    />
  );
}
