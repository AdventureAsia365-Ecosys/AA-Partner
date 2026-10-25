// web/app/(crm)/pipeline/[tripId]/page.tsx — A-03 trip workspace, Slice 1 (AA-936): header with stage, waiting on and
// owner; the party (names; e-mail / phone only on a click, each click audited); tasks; notes; the inquiry answers
// (no identity values) and the activity. One "view" audit event per load (API). AA-937: the trip's documents
// (one list_view of the file names), companions and dates. AA-943 (CRM-15): the Traveller info panel reads
// GET /trips/{id}/stage2 (completeness with the missing codes, the planning form's pages, the F2 answers); the
// planning form's link opens on the first client host. AA-944 (CRM-16): the Bookings tab under the workspace
// (GET /trips/{id}/lines, money for owner / agent only; the supplier list for the add form). AA-945 (CRM-17): the
// Messages tab (the trip's conversations, the composer with saved replies, autosaved drafts, Undo, held messages).
// AA-946 (CRM-18): the Proposal tab (GET /trips/{id}/lifecycle: proposals, payment schedule, portal progress; the
// published booking terms from GET /agreements); its client links open on the first client host. AA-947 (CRM-19): AA
// lines in the Bookings tab; the Lost picker pre-selects the reason the AA lines suggest (lost_reason_hint).
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { LinesPanel } from "@/components/ledger/LinesPanel";
import { MessagesPanel } from "@/components/mail/MessagesPanel";
import { ProposalPanel } from "@/components/proposals/ProposalPanel";
import { TripPanel } from "@/components/pipeline/TripPanel";
import type { DocumentRow } from "@/lib/clients";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { SuppliersView, TripLines } from "@/lib/ledger";
import type { Conversation, SavedReply } from "@/lib/mail";
import type { Member, Stage2View, TripView } from "@/lib/pipeline";
import type { Agreement, LifecycleView } from "@/lib/proposals";
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
  const tripQuery = new URLSearchParams({ trip_id: tripId });
  const [trip, members, docs, stage2, lines, suppliers, convs, replies, lifecycle, agreements] = await Promise.all([
    crmCall<TripView>(ctx.cfg, session, "GET", `trips/${tripId}`),
    crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members"),
    crmCall<{ documents: DocumentRow[] }>(ctx.cfg, session, "GET", `trips/${tripId}/documents`),
    crmCall<Stage2View>(ctx.cfg, session, "GET", `trips/${tripId}/stage2`),
    crmCall<TripLines>(ctx.cfg, session, "GET", `trips/${tripId}/lines`),
    crmCall<SuppliersView>(ctx.cfg, session, "GET", "suppliers"),
    crmCall<{ conversations: Conversation[] }>(ctx.cfg, session, "GET", "conversations", { query: tripQuery }),
    crmCall<{ saved_replies: SavedReply[] }>(ctx.cfg, session, "GET", "saved-replies"),
    crmCall<LifecycleView>(ctx.cfg, session, "GET", `trips/${tripId}/lifecycle`),
    crmCall<{ agreements: Agreement[] }>(ctx.cfg, session, "GET", "agreements"),
  ]);
  const clientHost = ctx.cfg.clientHosts[0];
  if (!trip.ok) {
    if (trip.status === 404) {
      return <ErrorState message={t(ctx.lang, "trip.notFound")} />;
    }
    return <ErrorState message={t(ctx.lang, "error.api")} />;
  }
  return (
    <div style={{ display: "grid", gap: 16 }}>
    <TripPanel
      view={trip.data}
      members={members.ok ? members.data.members : []}
      role={ctx.role}
      lang={ctx.lang}
      locale={ctx.locale}
      documents={docs.ok ? docs.data.documents : []}
      stage2={stage2.ok ? stage2.data : null}
      clientOrigin={clientHost ? `https://${clientHost}` : null}
      lostHint={lines.ok ? (lines.data.lost_reason_hint ?? null) : null}
    />
    {lifecycle.ok && (
      <ProposalPanel
        view={lifecycle.data}
        terms={agreements.ok ? agreements.data.agreements.filter((d) => d.kind === "booking_terms" && d.status === "published") : []}
        party={trip.data.party}
        role={ctx.role}
        lang={ctx.lang}
        locale={ctx.locale}
        clientOrigin={clientHost ? `https://${clientHost}` : null}
      />
    )}
    {lines.ok && (
      <LinesPanel
        tripId={tripId}
        view={lines.data}
        suppliers={suppliers.ok ? suppliers.data.suppliers : []}
        role={ctx.role}
        lang={ctx.lang}
        locale={ctx.locale}
      />
    )}
    {convs.ok && (
      <MessagesPanel
        tripId={tripId}
        conversations={convs.data.conversations}
        savedReplies={replies.ok ? replies.data.saved_replies : []}
        lang={ctx.lang}
        locale={ctx.locale}
      />
    )}
    </div>
  );
}
