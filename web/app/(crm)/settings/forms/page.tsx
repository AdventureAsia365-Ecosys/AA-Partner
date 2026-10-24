// web/app/(crm)/settings/forms/page.tsx — A-18 Settings > Forms & embed (AA-936): the agency's inquiry forms, their
// hosted link on the client host and the iframe embed code; the owner creates forms, turns them on or off and lists
// the websites allowed to frame them (CSP frame-ancestors on the form page).
import { ErrorState } from "@/app/_kit";
import { FormsAdmin, type FormRow } from "@/components/pipeline/FormsAdmin";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function FormsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const res = session ? await crmCall<{ forms: FormRow[] }>(ctx.cfg, session, "GET", "intake-forms") : null;
  return (
    <div data-testid="crm-settings-forms" data-card="AA-936" style={{ display: "grid", gap: 14, maxWidth: 900 }}>
      <a href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </a>
      <PageTitle title={t(ctx.lang, "forms.title")} subtitle={t(ctx.lang, "forms.subtitle")} />
      {!res || !res.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <FormsAdmin forms={res.data.forms} clientHosts={ctx.cfg.clientHosts} isOwner={ctx.role === "owner"} lang={ctx.lang} />
      )}
    </div>
  );
}
