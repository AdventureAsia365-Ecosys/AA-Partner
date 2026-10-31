// web/app/staff/(console)/support/page.tsx — S-04 start page (AA-1019 on the AA-971 API): crm-support sees the agencies
// whose owner allows AA support now and starts a session with a ticket; crm-ops opens emergency access (break-glass)
// with a reason, a ticket and 15 to 60 minutes. Each block shows only for its group (the API checks again).
import { ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { BreakglassStart } from "@/components/staff/BreakglassStart";
import { SupportStart } from "@/components/staff/SupportStart";
import { t } from "@/lib/i18n";
import { staffCall } from "@/lib/staffApi";
import { refusalKey, type BreakglassAgency, type GrantedAgency } from "@/lib/staffConsole";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

export default async function StaffSupportPage() {
  const { cfg, session, lang, locale } = await staffContext();
  if (!session) return <ErrorState message={t(lang, "staff.noAccess")} />;
  const failed = (status: number, detail: string) =>
    t(lang, status === 0 || status >= 500 ? "staff.error.api" : refusalKey(detail));
  const support = session.groups.includes("crm-support")
    ? await staffCall<{ items: GrantedAgency[] }>(cfg, session, "GET", "support/agencies")
    : null;
  const ops = session.groups.includes("crm-ops")
    ? await staffCall<{ items: BreakglassAgency[] }>(cfg, session, "GET", "breakglass/agencies")
    : null;
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-support">
      <PageTitle title={t(lang, "staff.support.title")} subtitle={t(lang, "staff.support.subtitle")} />
      {support && (
        <Panel title={t(lang, "staff.support.granted")} testId="staff-support-granted">
          {support.ok ? (
            <SupportStart agencies={support.data.items} lang={lang} locale={locale} />
          ) : (
            <ErrorState message={failed(support.status, support.detail)} />
          )}
        </Panel>
      )}
      {ops && (
        <Panel title={t(lang, "staff.breakglass.title")} testId="staff-breakglass-panel">
          {ops.ok ? (
            <BreakglassStart agencies={ops.data.items} lang={lang} locale={locale} />
          ) : (
            <ErrorState message={failed(ops.status, ops.detail)} />
          )}
        </Panel>
      )}
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{t(lang, "staff.audited")}</p>
    </div>
  );
}
