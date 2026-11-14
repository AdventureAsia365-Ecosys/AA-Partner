// web/app/(crm)/settings/forms/page.tsx — A-18 Settings > Forms & embed (AA-936): the agency's inquiry forms, their
// hosted link on the client host and the iframe embed code; the owner creates forms, turns them on or off and lists
// the websites allowed to frame them (CSP frame-ancestors on the form page). AA-943 (CRM-15): the owner's pipeline
// settings (timezone, Stage 2 auto-send, call windows; GET / PUT /settings/pipeline, owner only).
import Link from "next/link";
import { ErrorState } from "@/app/_kit";
import { FormsAdmin, PipelineSettingsPanel, type FormRow } from "@/components/pipeline/FormsAdmin";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { FormQuota, PipelineSettings } from "@/lib/intakeForms";
import type { TeamPage } from "@/lib/team";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

type Session = Awaited<ReturnType<typeof serverContext>>["session"];

/** The page's reads: the forms with the hourly quota (every role), and for the owner the pipeline settings (null when
 * the read failed: the panel says so) and the members a form's new inquiries may go to (AA-1096 S280). */
async function reads(cfg: Parameters<typeof crmCall>[0], session: Session, isOwner: boolean) {
  if (!session) return { res: null, settings: null, team: [] };
  const owner = <T,>(path: string) => (isOwner ? crmCall<T>(cfg, session, "GET", path) : Promise.resolve(null));
  const [res, settings, team] = await Promise.all([
    crmCall<{ forms: FormRow[] } & FormQuota>(cfg, session, "GET", "intake-forms"),
    owner<PipelineSettings>("settings/pipeline"),
    owner<TeamPage>("team"),
  ]);
  return { res, settings: settings?.ok ? settings.data : null, team: team?.ok ? team.data.members : [] };
}

export default async function FormsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const isOwner = ctx.role === "owner";
  const { res, settings, team } = await reads(ctx.cfg, session, isOwner);
  return (
    <div data-testid="crm-settings-forms" style={{ display: "grid", gap: 14, maxWidth: 900 }}>
      <Link href="/settings" style={{ fontSize: 13 }}>
        ← {t(ctx.lang, "settings.title")}
      </Link>
      <PageTitle title={t(ctx.lang, "forms.title")} subtitle={t(ctx.lang, "forms.subtitle")} />
      {!res || !res.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <FormsAdmin
          forms={res.data.forms}
          clientHosts={ctx.cfg.clientHosts}
          isOwner={isOwner}
          lang={ctx.lang}
          quota={res.data}
          team={team}
        />
      )}
      <PipelineSettingsPanel settings={settings} isOwner={isOwner} lang={ctx.lang} />
    </div>
  );
}
