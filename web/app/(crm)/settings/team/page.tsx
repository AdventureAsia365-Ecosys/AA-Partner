// web/app/(crm)/settings/team/page.tsx — A-18 Settings > Team & roles (AA-939, card CRM-11; design v3 §4.6). Owner
// only (the permission matrix: team_view / team_manage / reassign are the owner's); an agent or assistant sees who
// manages the team instead of the page. Members, invitations, the plan and the reassign queue come from /crm/v1.
import { ErrorState, K } from "@/app/_kit";
import { PageTitle, Panel } from "@/components/PageTitle";
import { TeamAdmin } from "@/components/team/TeamAdmin";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import type { ReassignQueue, TeamPage } from "@/lib/team";

export const dynamic = "force-dynamic";

export default async function TeamSettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const back = (
    <a href="/settings" style={{ fontSize: 13 }}>
      ← {t(ctx.lang, "settings.title")}
    </a>
  );
  if (ctx.role !== "owner") {
    return (
      <div data-testid="crm-settings-team" data-card="AA-939" style={{ display: "grid", gap: 14, maxWidth: 900 }}>
        {back}
        <PageTitle title={t(ctx.lang, "team.title")} />
        <Panel>
          <p style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="team-owner-only">
            {t(ctx.lang, "team.ownerOnly")}
          </p>
        </Panel>
      </div>
    );
  }
  const { session } = await serverContext();
  const [page, queue] = session
    ? await Promise.all([
        crmCall<TeamPage>(ctx.cfg, session, "GET", "team"),
        crmCall<ReassignQueue>(ctx.cfg, session, "GET", "team/reassign-queue"),
      ])
    : [null, null];
  return (
    <div data-testid="crm-settings-team" data-card="AA-939" style={{ display: "grid", gap: 14, maxWidth: 980 }}>
      {back}
      <PageTitle title={t(ctx.lang, "team.title")} subtitle={t(ctx.lang, "team.subtitle")} />
      {!page || !page.ok ? (
        <ErrorState message={t(ctx.lang, "error.api")} />
      ) : (
        <TeamAdmin page={page.data} queue={queue && queue.ok ? queue.data : null} lang={ctx.lang} locale={ctx.locale} />
      )}
    </div>
  );
}
