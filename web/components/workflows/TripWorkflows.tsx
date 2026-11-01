"use client";
// web/components/workflows/TripWorkflows.tsx — the trip's automations (card CRM-29a, AA-959; design v3 M10, R9 task
// lists, U5 "apply a workflow (preview)"): the runs on this trip (rule, running / finished / stopped and why, each step's
// outcome in words: "task created", "not sent: no marketing consent", ...) and, for the owner and agents, "Apply a task
// list": pick one of the agency's task lists, Check (the dry run says whether it fits this trip and why not), Apply.
// A running workflow can be stopped. Loaded in the browser after the page (GET /workflows/runs, GET /workflows).
import { useEffect, useState } from "react";
import { Badge, Button, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import { canApply, outcomeText, stopText, workflowErrorKey, type Rule, type Run, type WorkflowOverview } from "@/lib/workflows";

const RUN_TONE = { active: "info", completed: "success", stopped: "neutral", failed: "danger" } as const;

export function TripWorkflows({ tripId, role, lang, locale, timeZone }: { tripId: string; role: Role; lang: Lang; locale: DateLocale; timeZone: string }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [lists, setLists] = useState<Rule[]>([]);
  const [pick, setPick] = useState("");
  const [check, setCheck] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const applier = canApply(role);

  const [tick, setTick] = useState(0);
  const load = () => setTick((n) => n + 1);

  useEffect(() => {
    let stopped = false;
    void (async () => {
      const [r, w] = await Promise.all([
        bffSend<{ runs: Run[] }>("GET", `workflows/runs?trip_id=${tripId}`),
        applier ? bffSend<WorkflowOverview>("GET", "workflows") : Promise.resolve(null),
      ]);
      if (stopped) return;
      setRuns(r.ok ? r.data.runs : []);
      if (w?.ok) setLists(w.data.rules.filter((x) => x.trigger.kind === "manual" && x.status === "active"));
    })();
    return () => {
      stopped = true;
    };
  }, [tripId, applier, tick]);

  async function apply(dryRun: boolean) {
    if (!pick) return;
    setBusy(true);
    const res = await bffSend<{ eligible: string[]; blocked: { reason: string }[] }>("POST", `workflows/${pick}/apply`, { trip_ids: [tripId], dry_run: dryRun });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(workflowErrorKey(res.detail)) });
      return;
    }
    const blocked = res.data.blocked[0]?.reason;
    if (dryRun) {
      setCheck(blocked ? fmt(tr("wf.applyBlocked"), { reason: tr(`wf.block.${blocked}`) }) : tr("wf.applyEligible"));
      return;
    }
    setCheck(null);
    setNotice({ tone: "success", text: tr("wf.applied") });
    window.setTimeout(load, 25_000);
  }

  async function stop(run: Run) {
    setBusy(true);
    const res = await bffSend("POST", `workflows/runs/${run.workflow_run_id}/stop`);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(workflowErrorKey(res.detail)) });
      return;
    }
    load();
  }

  return (
    <Panel title={tr("wf.tripTitle")} testId="trip-workflows">
      <div style={{ display: "grid", gap: 12 }}>
        {notice && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
            {notice.text}
          </p>
        )}
        {runs !== null && runs.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("wf.tripEmpty")}</p>}
        {runs && runs.length > 0 && (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {runs.map((run) => (
              <li key={run.workflow_run_id} data-testid="trip-workflow-run" data-status={run.status} style={{ display: "grid", gap: 4 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 13, overflowWrap: "anywhere" }}>{run.rule_name}</strong>
                  <Badge tone={RUN_TONE[run.status]}>{tr(`wf.run.status.${run.status}`)}</Badge>
                  {run.stop_reason && <span style={{ fontSize: 12, color: K.muted }}>{stopText(lang, run.stop_reason)}</span>}
                  {run.status === "active" && run.next_at && <span style={{ fontSize: 12, color: K.muted }}>{fmt(tr("wf.run.next"), { when: formatInstant(run.next_at, locale, timeZone) })}</span>}
                  {run.status === "active" && applier && (
                    <Button size="sm" variant="ghost" disabled={busy || !hydrated} onClick={() => void stop(run)}>
                      {tr("wf.stopRun")}
                    </Button>
                  )}
                </div>
                {run.log.length > 0 && (
                  <ol style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: K.muted }}>
                    {run.log.map((e, i) => (
                      <li key={i} data-outcome={e.outcome}>
                        {tr(`wf.action.${e.action}`)}: {outcomeText(lang, e.outcome)}
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ul>
        )}
        {applier && (
          <div data-testid="trip-apply" style={{ display: "grid", gap: 6 }}>
            <strong style={{ fontSize: 13 }}>{tr("wf.applyTitle")}</strong>
            {lists.length === 0 ? (
              <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("wf.applyNone")}</p>
            ) : (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <span style={{ minWidth: 220 }}>
                  <Select
                    size="sm"
                    aria-label={tr("wf.applyPick")}
                    value={pick}
                    placeholder={tr("wf.applyPick")}
                    onChange={(e) => {
                      setPick(e.target.value);
                      setCheck(null);
                    }}
                    options={lists.map((x) => ({ value: x.workflow_rule_id, label: x.name }))}
                  />
                </span>
                <Button size="sm" variant="secondary" disabled={!pick || busy || !hydrated} onClick={() => void apply(true)}>
                  {tr("wf.applyCheck")}
                </Button>
                <Button size="sm" variant="primary" disabled={!pick || busy || !hydrated || check === null || check !== tr("wf.applyEligible")} onClick={() => void apply(false)}>
                  {tr("wf.applyRun")}
                </Button>
              </div>
            )}
            {check && <span style={{ fontSize: 12.5, color: check === tr("wf.applyEligible") ? K.success : K.danger }}>{check}</span>}
          </div>
        )}
      </div>
    </Panel>
  );
}
