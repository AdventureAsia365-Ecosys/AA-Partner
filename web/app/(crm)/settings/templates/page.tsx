// web/app/(crm)/settings/templates/page.tsx — Settings > Message templates (AA-951, card CRM-22, R9): the agency's
// template library and the AA starter templates not added yet.
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { MessageTemplates } from "@/components/settings/MessageTemplates";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import type { TemplateLibrary } from "@/lib/textProposal";

export const dynamic = "force-dynamic";

export default async function TemplatesPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const library = await crmCall<TemplateLibrary>(ctx.cfg, session, "GET", "message-templates");
  return (
    <div data-testid="crm-templates" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <a href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </a>
      <PageTitle title={t(ctx.lang, "templates.title")} subtitle={t(ctx.lang, "templates.subtitle")} />
      {!library.ok ? <ErrorState message={t(ctx.lang, "error.api")} /> : <MessageTemplates library={library.data} role={ctx.role} lang={ctx.lang} />}
    </div>
  );
}
