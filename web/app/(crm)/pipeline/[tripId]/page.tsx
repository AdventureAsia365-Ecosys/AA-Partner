// web/app/(crm)/pipeline/[tripId]/page.tsx — A-03 trip workspace, Slice 1 (AA-936): header with stage, waiting on and
// owner; the party (names; e-mail / phone only on a click, each click audited); tasks; notes; the inquiry answers
// (no identity values) and the activity. One "view" audit event per load (API). AA-937: the trip's documents
// (one list_view of the file names), companions and dates. AA-943 (CRM-15): the Traveller info panel reads
// GET /trips/{id}/stage2 (completeness with the missing codes, the planning form's pages, the F2 answers); the
// planning form's link opens on the first client host. AA-944 (CRM-16): the Bookings tab under the workspace
// (GET /trips/{id}/lines, money for owner / agent only; the supplier list for the add form). AA-945 (CRM-17): the
// Messages tab (the trip's conversations, the composer with saved replies, autosaved drafts, Undo, held messages).
// AA-946 (CRM-18): the Proposal tab (GET /trips/{id}/lifecycle: proposals, payment schedule, portal progress; the
// booking terms versions from GET /agreements); its client links open on the first client host. AA-947 (CRM-19): AA
// lines in the Bookings tab; the Lost picker pre-selects the reason the AA lines suggest (lost_reason_hint). CRM-23
// slice 2: the Invoices card under the Proposal tab (GET /trips/{id}/invoices; due schedule rows from the lifecycle).
// AA-955 (CRM-25): the Signed documents panel (GET /trips/{id}/agreements: who signed, guardian, insurance choice,
// seal, the terms acceptances and the re-accept state, the signing log). AA-961 (CRM-30): the AA incident banner
// (GET /trips/{id}/incidents: open flags on the trip's departures, and those cleared in the last 14 days). S262
// (AA-957 live): a cancelled trip's Proposal and Invoices cards are read-only (no new payment, invoice or client link).
// S262 round 3: the trip is closed on every card (no new AA item, line, date, shared document, signature or proposal;
// messages stay open), and the cancel run closes every card at once (TripClosedProvider), not only the header.
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { TripIncidentBanner } from "@/components/duty/IncidentPanel";
import { InvoicesPanel } from "@/components/invoices/InvoicesPanel";
import { LinesPanel } from "@/components/ledger/LinesPanel";
import { MessagesPanel } from "@/components/mail/MessagesPanel";
import { ProposalPanel } from "@/components/proposals/ProposalPanel";
import { TripAgreementsPanel } from "@/components/proposals/TripAgreementsPanel";
import { OpsReleasePanel } from "@/components/pipeline/OpsReleasePanel";
import { TripClosedProvider } from "@/components/pipeline/TripClosed";
import { TripPanel } from "@/components/pipeline/TripPanel";
import type { DocumentRow } from "@/lib/clients";
import type { TripIncident } from "@/lib/duty";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import type { OpsReleaseView } from "@/lib/opsRelease";
import { t } from "@/lib/i18n";
import type { Invoice } from "@/lib/invoices";
import type { SuppliersView, TripLines } from "@/lib/ledger";
import type { Conversation, SavedReply } from "@/lib/mail";
import { tripClosedForBilling, type Member, type Stage2View, type TripView } from "@/lib/pipeline";
import type { Agreement, LifecycleView, TripAgreements } from "@/lib/proposals";
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
  const [trip, members, docs, stage2, lines, suppliers, convs, replies, lifecycle, agreements, invoices, signed, incidents, ops] = await Promise.all([
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
    crmCall<{ invoices: Invoice[]; timezone?: string }>(ctx.cfg, session, "GET", `trips/${tripId}/invoices`),
    crmCall<TripAgreements>(ctx.cfg, session, "GET", `trips/${tripId}/agreements`),
    crmCall<{ incidents: TripIncident[] }>(ctx.cfg, session, "GET", `trips/${tripId}/incidents`),
    crmCall<OpsReleaseView>(ctx.cfg, session, "GET", `trips/${tripId}/ops-release`),
  ]);
  const clientHost = ctx.cfg.clientHosts[0];
  // The agency's zone for times on the page (S262 AA-957: a time printed in the server's zone on the server and the
  // browser's in the browser failed hydration, and React's client re-render dropped the dark theme).
  const agencyZone = invoices.ok ? (invoices.data.timezone ?? "UTC") : "UTC";
  if (!trip.ok) {
    if (trip.status === 404) {
      return <ErrorState message={t(ctx.lang, "trip.notFound")} />;
    }
    return <ErrorState message={t(ctx.lang, "error.api")} />;
  }
  // S262 (C12 §2.7): a cancelled trip, or one whose cancellation waits for AA, bills the client no more.
  const closed = tripClosedForBilling(trip.data.trip.stage, trip.data.cancellation);
  return (
    <TripClosedProvider closed={closed}>
    <div style={{ display: "grid", gap: 16 }}>
    {incidents.ok && <TripIncidentBanner incidents={incidents.data.incidents} lang={ctx.lang} locale={ctx.locale} />}
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
      timeZone={agencyZone}
    />
    {lifecycle.ok && (
      <ProposalPanel
        view={lifecycle.data}
        terms={agreements.ok ? agreements.data.agreements.filter((d) => d.kind === "booking_terms") : []}
        party={trip.data.party}
        role={ctx.role}
        lang={ctx.lang}
        locale={ctx.locale}
        clientOrigin={clientHost ? `https://${clientHost}` : null}
        timeZone={agencyZone}
        closed={closed}
      />
    )}
    {invoices.ok && (
      <InvoicesPanel
        tripId={tripId}
        invoices={invoices.data.invoices}
        schedule={lifecycle.ok ? lifecycle.data.schedule : []}
        role={ctx.role}
        lang={ctx.lang}
        locale={ctx.locale}
        timeZone={invoices.data.timezone ?? "UTC"}
        closed={closed}
      />
    )}
    {ops.ok && <OpsReleasePanel tripId={tripId} view={ops.data} lang={ctx.lang} locale={ctx.locale} canOpenAccessLog={ctx.role === "owner"} />}
    {signed.ok && <TripAgreementsPanel view={signed.data} party={trip.data.party} lang={ctx.lang} locale={ctx.locale} closed={closed} />}
    {lines.ok && (
      <LinesPanel
        tripId={tripId}
        view={lines.data}
        suppliers={suppliers.ok ? suppliers.data.suppliers : []}
        role={ctx.role}
        lang={ctx.lang}
        locale={ctx.locale}
        closed={closed}
      />
    )}
    {convs.ok && (
      <MessagesPanel
        tripId={tripId}
        conversations={convs.data.conversations}
        savedReplies={replies.ok ? replies.data.saved_replies : []}
        lang={ctx.lang}
        locale={ctx.locale}
        closed={closed}
      />
    )}
    </div>
    </TripClosedProvider>
  );
}
