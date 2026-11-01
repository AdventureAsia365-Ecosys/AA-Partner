// web/app/(crm)/automations/page.tsx — A-17 Automations (card CRM-29a, AA-959; design v3 M10, R9): the agency's rules
// and the AA library from GET /crm/v1/workflows. For the owner (who edits) also the message templates, the approved
// SMS / WhatsApp templates and the members the editor offers. Every role reads; the API refuses edits by others.
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { AutomationsBoard } from "@/components/workflows/AutomationsBoard";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { MessagingView } from "@/lib/messaging";
import type { Member } from "@/lib/pipeline";
import { serverContext } from "@/lib/serverContext";
import type { TemplateLibrary } from "@/lib/textProposal";
import type { WorkflowOverview } from "@/lib/workflows";

export const dynamic = "force-dynamic";

export default async function AutomationsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const owner = ctx.role === "owner";
  const [view, templates, messaging, members] = await Promise.all([
    crmCall<WorkflowOverview>(ctx.cfg, session, "GET", "workflows"),
    owner ? crmCall<TemplateLibrary>(ctx.cfg, session, "GET", "message-templates") : Promise.resolve(null),
    owner ? crmCall<MessagingView>(ctx.cfg, session, "GET", "messaging") : Promise.resolve(null),
    owner ? crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members") : Promise.resolve(null),
  ]);
  if (!view.ok) return <ErrorState message={t(ctx.lang, "error.api")} />;
  const approved = messaging?.ok ? (messaging.data.templates ?? []).filter((x) => x.approval === "approved") : [];
  return (
    <div data-testid="crm-automations" data-crm-toast-room="" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 4, maxWidth: 1100 }}>
      <PageTitle title={t(ctx.lang, "nav.automations")} subtitle={t(ctx.lang, "nav.automations.subtitle")} />
      <AutomationsBoard
        view={view.data}
        templates={templates?.ok ? templates.data.templates : []}
        textTemplates={approved}
        members={members?.ok ? members.data.members.filter((m) => m.status === "active") : []}
        lang={ctx.lang}
      />
    </div>
  );
}
