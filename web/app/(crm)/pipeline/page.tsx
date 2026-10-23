// web/app/(crm)/pipeline/page.tsx — A-02 Pipeline, the board (AA-936, card CRM-08; design v3 WF-1, ui-mockups
// "Pipeline"). The API decrypts the lead's display name only and writes one list_view audit event per load; cards
// never show margin, e-mail or phone. Filters are links (?waiting_on=…&owner=me), so the page stays a server render.
import { EmptyState, ErrorState } from "@/app/_kit";
import { BoardView } from "@/components/pipeline/BoardView";
import { PageTitle } from "@/components/PageTitle";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { dictionary, t } from "@/lib/i18n";
import { parseFilter, type Board } from "@/lib/pipeline";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function PipelinePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  const filter = parseFilter(await searchParams);
  const query = new URLSearchParams();
  if (filter.waiting_on) query.set("waiting_on", filter.waiting_on);
  if (filter.owner === "me") query.set("owner", "me");
  const res = session ? await crmCall<Board>(ctx.cfg, session, "GET", "pipeline", { query }) : null;
  const { lang } = ctx;
  return (
    <div data-testid="crm-pipeline" data-card="AA-936" style={{ display: "grid", gap: 14, minWidth: 0 }}>
      <PageTitle title={t(lang, "pipeline.title")} subtitle={t(lang, "pipeline.subtitle")} />
      {!res || !res.ok ? (
        <ErrorState message={t(lang, "pipeline.error")} />
      ) : res.data.total === 0 && !filter.waiting_on && filter.owner === "anyone" ? (
        <EmptyState
          title={t(lang, "section.pipeline.title")}
          description={t(lang, "section.pipeline.body")}
          action={<a href="/settings/forms">{t(lang, "pipeline.formLink")}</a>}
        />
      ) : (
        <BoardView board={res.data} lang={lang} locale={ctx.locale} dict={dictionary(lang)} />
      )}
    </div>
  );
}
