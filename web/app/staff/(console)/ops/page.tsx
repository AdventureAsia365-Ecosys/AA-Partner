// web/app/staff/(console)/ops/page.tsx — S-02 board (AA-1019; API GET /crm/staff/v1/ops-releases, group crm-ops) and
// AA-1144 the agencies' open requests for extra hotel nights (GET /crm/staff/v1/aa-extra-nights).
// One `ops_release_list` event per agency listed is written by the API (the agencies see "AA ops listed ...").
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { ExtraNightsBoard, type StaffExtraNight } from "@/components/staff/ExtraNightsBoard";
import { OpsBoard } from "@/components/staff/OpsBoard";
import { t } from "@/lib/i18n";
import { staffCall } from "@/lib/staffApi";
import { boardDays, refusalKey, type Board } from "@/lib/staffConsole";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

export default async function StaffOpsPage({ searchParams }: { searchParams: Promise<{ days?: string | string[] }> }) {
  const { days: raw } = await searchParams;
  const { cfg, session, lang, locale } = await staffContext();
  const days = boardDays(raw);
  const title = <PageTitle title={t(lang, "staff.ops.title")} subtitle={t(lang, "staff.ops.subtitle")} />;
  if (!session || !session.groups.includes("crm-ops")) {
    return (
      <>
        {title}
        <ErrorState message={t(lang, "staff.noAccess")} />
      </>
    );
  }
  const res = await staffCall<Board>(cfg, session, "GET", "ops-releases", { query: new URLSearchParams({ days: String(days) }) });
  // AA-1144 (B11): the agencies' open requests for extra hotel nights, under the board (its own error line).
  const nights = await staffCall<{ items: StaffExtraNight[] }>(cfg, session, "GET", "aa-extra-nights");
  return (
    <>
      {title}
      {res.ok ? (
        <OpsBoard board={res.data} days={days} lang={lang} locale={locale} />
      ) : (
        <ErrorState message={t(lang, res.status === 0 || res.status >= 500 ? "staff.error.api" : refusalKey(res.detail))} />
      )}
      {nights.ok ? (
        <ExtraNightsBoard items={nights.data.items} lang={lang} locale={locale} />
      ) : (
        <ErrorState message={t(lang, "staff.extraNights.error")} />
      )}
    </>
  );
}
