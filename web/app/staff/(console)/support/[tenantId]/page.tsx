// web/app/staff/(console)/support/[tenantId]/page.tsx — S-04, one agency inside the caller's live support or emergency
// session (AA-1019 on the AA-971 API): the banner (amber support / red emergency, End session), the agency overview
// (settings codes, trips per stage) and its trips, read-only. Every read is checked again by the API (a grant the owner
// ended answers 403 support_session_ended on the very next page) and written to the agency's access log.
import Link from "next/link";
import { notFound } from "next/navigation";
import { ErrorState, K } from "@/app/_kit";
import { Panel, PageTitle } from "@/components/PageTitle";
import { AgencyTrips } from "@/components/staff/AgencyTrips";
import { SessionBanner } from "@/components/staff/SessionBanner";
import { SessionHistory } from "@/components/staff/SessionHistory";
import { codeLabel } from "@/lib/accessLog";
import { t } from "@/lib/i18n";
import { staffCall } from "@/lib/staffApi";
import { liveSession, refusalKey, TRIPS_PAGE, uuidParam, type MySessions, type Overview, type TripsPage } from "@/lib/staffConsole";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(110px, 180px) minmax(0, 1fr)", gap: 8, fontSize: 13 }}>
      <span style={{ color: K.muted }}>{label}</span>
      <span style={{ color: K.body, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

function settingValue(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export default async function StaffAgencyPage({ params }: { params: Promise<{ tenantId: string }> }) {
  const tenantId = uuidParam((await params).tenantId);
  if (!tenantId) notFound();
  const { cfg, session, lang, locale } = await staffContext();
  if (!session) return <ErrorState message={t(lang, "staff.noAccess")} />;
  const back = (
    <Link href="/staff/support" style={{ fontSize: 13 }}>
      ← {t(lang, "staff.back")}
    </Link>
  );
  const mine = await staffCall<MySessions>(cfg, session, "GET", `agencies/${tenantId}/session`);
  if (!mine.ok) {
    if (mine.status === 404) notFound();
    return (
      <div style={{ display: "grid", gap: 12 }}>
        {back}
        <ErrorState message={t(lang, mine.status === 0 || mine.status >= 500 ? "staff.error.api" : refusalKey(mine.detail))} />
      </div>
    );
  }
  const agencyName = mine.data.agency_name ?? tenantId.slice(0, 8);
  const live = liveSession(mine.data.sessions);
  const history = (
    <Panel title={t(lang, "staff.session.history")}>
      <SessionHistory sessions={mine.data.sessions} lang={lang} locale={locale} />
    </Panel>
  );
  const overview = live ? await staffCall<Overview>(cfg, session, "GET", `agencies/${tenantId}/overview`) : null;
  if (!live || !overview || !overview.ok) {
    const detail = overview && !overview.ok ? overview.detail : "no_support_session";
    return (
      <div style={{ display: "grid", gap: 12 }} data-testid="staff-agency-refused">
        {back}
        <PageTitle title={agencyName} />
        <ErrorState message={t(lang, refusalKey(detail))} />
        {mine.data.sessions.length > 0 && history}
      </div>
    );
  }
  const trips = await staffCall<TripsPage>(cfg, session, "GET", `agencies/${tenantId}/trips`, {
    query: new URLSearchParams({ limit: String(TRIPS_PAGE) }),
  });
  const o = overview.data;
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="staff-agency">
      {back}
      <SessionBanner session={live} agencyName={o.agency_name} lang={lang} locale={locale} />
      <PageTitle title={o.agency_name} subtitle={t(lang, "staff.audited")} />
      <Panel title={t(lang, "staff.agency.overview")}>
        <div style={{ display: "grid", gap: 6 }}>
          <Row label={t(lang, "staff.agency.status")}>{o.status}</Row>
          <Row label={t(lang, "staff.agency.locale")}>{o.default_locale}</Row>
          <Row label={t(lang, "staff.agency.pipeline")}>
            {Object.entries(o.pipeline)
              .map(([stage, n]) => `${codeLabel(lang, "stage", stage)} ${n}`)
              .join(" · ") || "—"}
          </Row>
        </div>
      </Panel>
      {o.settings && (
        <Panel title={t(lang, "staff.agency.settings")}>
          <div style={{ display: "grid", gap: 4 }}>
            {Object.entries(o.settings).map(([key, value]) => (
              <Row key={key} label={key.replace(/_/g, " ")}>
                <span style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}>{settingValue(value)}</span>
              </Row>
            ))}
          </div>
        </Panel>
      )}
      <Panel title={t(lang, "staff.agency.trips")}>
        {trips.ok ? (
          <AgencyTrips tenantId={tenantId} first={trips.data} lang={lang} locale={locale} />
        ) : (
          <ErrorState message={t(lang, refusalKey(trips.detail))} />
        )}
      </Panel>
      {history}
    </div>
  );
}
