// web/app/(crm)/pipeline/[tripId]/proposal/[proposalId]/text/page.tsx — A-05, the text proposal of one version
// (AA-951, card CRM-22). Reads GET /proposals/{id}/text (e-mail, in the person's UI language; one "view" audit event)
// and the template library; the page is a client component from there on (components/proposals/TextProposalPage.tsx),
// every call through the BFF lines of card AA-951 in lib/bff.ts.
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { TextProposalPage } from "@/components/proposals/TextProposalPage";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import { textErrorKey, type TemplateLibrary, type TextPreview } from "@/lib/textProposal";

export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function TextProposalRoute({ params }: { params: Promise<{ tripId: string; proposalId: string }> }) {
  const { tripId, proposalId } = await params;
  if (!UUID.test(tripId) || !UUID.test(proposalId)) notFound();
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const [preview, library] = await Promise.all([
    crmCall<TextPreview>(ctx.cfg, session, "GET", `proposals/${proposalId}/text`, {
      query: new URLSearchParams({ format: "email", locale: ctx.lang }),
    }),
    crmCall<TemplateLibrary>(ctx.cfg, session, "GET", "message-templates"),
  ]);
  if (!preview.ok) {
    if (preview.status === 404) return <ErrorState message={t(ctx.lang, "builder.notFound")} />;
    if (preview.status === 409) return <ErrorState message={t(ctx.lang, textErrorKey(preview.detail))} />;
    return <ErrorState message={t(ctx.lang, "error.api")} />;
  }
  if (preview.data.trip_id !== tripId) notFound();
  return <TextProposalPage initial={preview.data} templates={library.ok ? library.data.templates : []} role={ctx.role} lang={ctx.lang} />;
}
