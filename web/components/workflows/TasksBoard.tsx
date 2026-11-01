"use client";
// web/components/workflows/TasksBoard.tsx — A-09 Tasks (card CRM-29a, AA-959; design v3 WF-10, R20, R44 step 1): my /
// the team's open tasks in the buckets Overdue / Today / Next 7 days / Later / Waiting for dates (in the agency's
// timezone, from GET /tasks), or what was done in the last 7 days. Each line: the task (its own title, or the label of
// its kind), the source (automation, own task, AA event, CRM), the trip, the due time ("date set by hand" when a member
// edited it), the assignee (team view: reassign), Done / Reopen. "New task" adds a member's own task; the T-48 h flight
// task opens the trip's arrival flights with a status link; the owner switches the morning digest on or off.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, Field, Input, K, Modal, Select, Switch, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { FilterTabs } from "@/components/FilterTabs";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, plural, t, type Lang, type MessageKey } from "@/lib/i18n";
import { memberLabel, type Member } from "@/lib/pipeline";
import { BUCKETS, taskTitle, workflowErrorKey, type TaskItem, type TaskList } from "@/lib/workflows";

export type TaskScope = "mine" | "team" | "done";

const SOURCE_TONE = { workflow: "info", manual: "neutral", aa_event: "warning", system: "neutral" } as const;

function Line({
  task,
  lang,
  locale,
  timeZone,
  members,
  team,
  busy,
  onRun,
}: {
  task: TaskItem;
  lang: Lang;
  locale: DateLocale;
  timeZone: string;
  members: Member[];
  team: boolean;
  busy: boolean;
  onRun: (method: "POST" | "PATCH", path: string, body: unknown) => Promise<boolean>;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [flights, setFlights] = useState<TaskItem["flights"] | null>(null);
  const open = task.status === "open";
  return (
    <li data-testid="task-line" data-kind={task.kind} data-source={task.source} style={{ display: "grid", gap: 6, padding: "10px 0", borderTop: `1px solid ${K.line}` }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "space-between" }}>
        <span style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", minWidth: 0 }}>
          {task.priority === "urgent" && open && <Badge tone="danger">{tr("tasks.priority.urgent")}</Badge>}
          <span style={{ fontSize: 13.5, color: open ? K.ink : K.muted, textDecoration: open ? "none" : "line-through", overflowWrap: "anywhere" }}>
            {taskTitle(lang, task)}
          </span>
          <Badge tone={SOURCE_TONE[task.source]}>{tr(`tasks.source.${task.source}`)}</Badge>
        </span>
        <span data-testid="task-toggle">
          <Button size="sm" variant={open ? "secondary" : "ghost"} disabled={busy} onClick={() => void onRun("POST", `tasks/${task.task_id}/status`, { status: open ? "done" : "open" })}>
            {open ? tr("tasks.done") : tr("tasks.reopen")}
          </Button>
        </span>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12.5, color: K.muted }}>
        {task.trip_id ? (
          <Link href={`/pipeline/${task.trip_id}`} style={{ color: K.accent, overflowWrap: "anywhere" }}>
            {task.trip_title ?? tr("tasks.openTrip")}
          </Link>
        ) : task.traveler_id ? (
          <Link href={`/clients/${task.traveler_id}`} style={{ color: K.accent }}>
            {tr("tasks.clientTask")}
          </Link>
        ) : (
          <span>{tr("tasks.noTrip")}</span>
        )}
        <span>
          {task.due_at ? formatInstant(task.due_at, locale, timeZone) : tr("tasks.dueNone")}
          {task.due_overridden ? ` · ${tr("tasks.dueEdited")}` : ""}
        </span>
        {team && open && members.length > 0 && (
          <span style={{ minWidth: 150 }}>
            <Select
              size="sm"
              aria-label={tr("tasks.assignee")}
              value={task.assignee_member_id ?? ""}
              disabled={busy}
              onChange={(e) => void onRun("PATCH", `tasks/${task.task_id}`, { assignee_member_id: e.target.value || null })}
              options={[{ value: "", label: tr("tasks.unassigned") }, ...members.map((m) => ({ value: m.member_id, label: memberLabel(lang, m) }))]}
            />
          </span>
        )}
        {task.kind === "confirm_arrival_flight" && open && (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={async () => {
              const res = await bffSend<TaskItem>("GET", `tasks/${task.task_id}`);
              setFlights(res.ok ? (res.data.flights ?? []) : []);
            }}
          >
            {tr("tasks.flights")}
          </Button>
        )}
      </div>
      {flights && (
        <ul data-testid="task-flights" style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, display: "grid", gap: 2 }}>
          {flights.length === 0 && <li>{tr("tasks.noFlights")}</li>}
          {flights.map((f) => (
            <li key={`${f.flight_no}:${f.sched_local}`}>
              {f.status_url ? (
                <a href={f.status_url} target="_blank" rel="noopener noreferrer" style={{ color: K.accent }}>
                  {fmt(tr("tasks.flightStatus"), { flight: f.flight_no })}
                </a>
              ) : (
                f.flight_no
              )}{" "}
              · {f.airport_iata} · {f.sched_local.replace("T", " ").slice(0, 16)}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function NewTask({ lang, locale, members, onClose, onSaved }: { lang: Lang; locale: DateLocale; members: Member[]; onClose: () => void; onSaved: () => void }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Modal open onClose={onClose} title={tr("tasks.new")} testId="task-new">
      <form
        action={async (data) => {
          const due = String(data.get("due_on") ?? "");
          const body: Record<string, unknown> = {
            title: String(data.get("title") ?? "").trim(),
            priority: String(data.get("priority") ?? "normal"),
            assignee_member_id: String(data.get("assignee") ?? "") || null,
          };
          if (due) body.due_on = due;
          setBusy(true);
          const res = await bffSend("POST", "tasks", body);
          setBusy(false);
          if (!res.ok) {
            if (!res.redirecting) setError(tr(workflowErrorKey(res.detail)));
            return;
          }
          onSaved();
        }}
        style={{ display: "grid", gap: 12 }}
      >
        <Field label={tr("tasks.newTitle")} hint={tr("tasks.newTitleHint")} required>
          <Input name="title" required maxLength={120} />
        </Field>
        <DateInput locale={locale} name="due_on" label={tr("tasks.dueDate")} readAs={tr("tasks.due")} invalid={tr("wf.error.generic")} />
        <Field label={tr("tasks.priority")}>
          <Select name="priority" defaultValue="normal" options={[{ value: "normal", label: tr("tasks.priority.normal") }, { value: "urgent", label: tr("tasks.priority.urgent") }]} />
        </Field>
        <Field label={tr("tasks.assignee")}>
          <Select name="assignee" defaultValue="" options={[{ value: "", label: tr("tasks.assigneeMe") }, ...members.filter((m) => !m.is_me).map((m) => ({ value: m.member_id, label: memberLabel(lang, m) }))]} />
        </Field>
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
            {error}
          </p>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button type="submit" variant="primary" disabled={!hydrated || busy}>
            {tr("tasks.save")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {tr("tasks.cancel")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function TasksBoard({ list, scope, members, lang, locale }: { list: TaskList; scope: TaskScope; members: Member[]; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [digest, setDigest] = useState(list.digest);

  async function run(method: "POST" | "PATCH" | "PUT", path: string, body: unknown): Promise<boolean> {
    setBusy(true);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice(tr(workflowErrorKey(res.detail)));
      return false;
    }
    setNotice(null);
    router.refresh();
    return true;
  }

  const tabs = (["mine", "team", "done"] as const).map((key) => ({ key, label: tr(`tasks.scope.${key}`), href: `/tasks?scope=${key}` }));
  const total = list.counts ? Object.values(list.counts).reduce((a, b) => a + b, 0) : (list.done?.length ?? 0);
  return (
    <div data-testid="tasks-board" data-scope={scope} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
        <FilterTabs tabs={tabs} active={scope} ariaLabel={tr("tasks.scopeLabel")} testId="tasks-scope" />
        <span data-testid="tasks-new-open">
          <Button variant="primary" size="sm" disabled={!hydrated} onClick={() => setAdding(true)}>
            {tr("tasks.new")}
          </Button>
        </span>
      </div>
      {list.can_set_digest && (
        <div data-testid="tasks-digest" style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Switch
            checked={digest}
            label={tr("tasks.digest")}
            disabled={busy || !hydrated}
            onChange={async (next) => {
              if (await run("PUT", "tasks/digest", { enabled: next })) setDigest(next);
            }}
          />
          <span style={{ fontSize: 13 }}>{tr("tasks.digest")}</span>
          <span style={{ fontSize: 12, color: K.muted, flexBasis: "100%" }}>{tr("tasks.digestHint")}</span>
        </div>
      )}
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {notice}
        </p>
      )}
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{plural(lang, "tasks.count", total)}</p>
      {total === 0 && <EmptyState title={tr("tasks.empty")} description={tr("tasks.emptyAll")} />}
      {list.buckets &&
        BUCKETS.filter((b) => (list.buckets?.[b].length ?? 0) > 0).map((b) => (
          <Panel key={b} title={`${tr(`tasks.bucket.${b}`)} (${list.buckets?.[b].length ?? 0})`} testId={`tasks-bucket-${b}`}>
            {b === "waiting_for_dates" && <p style={{ margin: "0 0 6px", fontSize: 12.5, color: K.muted }}>{tr("tasks.bucket.waitingHint")}</p>}
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {list.buckets?.[b].map((task) => (
                <Line key={task.task_id} task={task} lang={lang} locale={locale} timeZone={list.timezone} members={members} team={scope === "team"} busy={busy} onRun={run} />
              ))}
            </ul>
          </Panel>
        ))}
      {list.done && list.done.length > 0 && (
        <Panel title={tr("tasks.scope.done")} testId="tasks-done">
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {list.done.map((task) => (
              <Line key={task.task_id} task={task} lang={lang} locale={locale} timeZone={list.timezone} members={members} team={false} busy={busy} onRun={run} />
            ))}
          </ul>
        </Panel>
      )}
      {adding && (
        <NewTask
          lang={lang}
          locale={locale}
          members={members}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            setNotice(null);
            router.refresh();
          }}
        />
      )}
    </div>
  );
}
