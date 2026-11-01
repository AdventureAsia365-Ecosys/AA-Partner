// web/app/(crm)/tasks/page.tsx — A-09 Tasks (card CRM-29a, AA-959; design v3 WF-10, R20): my tasks (default), the team's,
// or what was done in the last 7 days, from GET /crm/v1/tasks (buckets in the agency's timezone), with the members for
// the assign menu (GET /members). Every role: the assistant works the same trips (design §4.6 "Create / edit trips").
import { ErrorState } from "@/app/_kit";
import { PageTitle } from "@/components/PageTitle";
import { TasksBoard, type TaskScope } from "@/components/workflows/TasksBoard";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { Member } from "@/lib/pipeline";
import { serverContext } from "@/lib/serverContext";
import type { TaskList } from "@/lib/workflows";

export const dynamic = "force-dynamic";

const SCOPES: readonly TaskScope[] = ["mine", "team", "done"];

export default async function TasksPage({ searchParams }: { searchParams: Promise<{ scope?: string }> }) {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const { scope: raw } = await searchParams;
  const scope: TaskScope = SCOPES.includes(raw as TaskScope) ? (raw as TaskScope) : "mine";
  const query = new URLSearchParams(scope === "done" ? { scope: "mine", status: "done" } : { scope });
  const [list, members] = await Promise.all([
    crmCall<TaskList>(ctx.cfg, session, "GET", "tasks", { query }),
    crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members"),
  ]);
  if (!list.ok) return <ErrorState message={t(ctx.lang, "error.api")} />;
  return (
    <div data-testid="crm-tasks" data-crm-toast-room="" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 4, maxWidth: 1100 }}>
      <PageTitle title={t(ctx.lang, "tasks.title")} subtitle={t(ctx.lang, "tasks.subtitle")} />
      <TasksBoard list={list.data} scope={scope} members={members.ok ? members.data.members.filter((m) => m.status === "active") : []} lang={ctx.lang} locale={ctx.locale} />
    </div>
  );
}
