"use client";
// web/components/tags/CoveragePanel.tsx — "N trips missing workflow Y [Preview] [Run]" (card CRM-29b, AA-960; design v3
// R30, WF-1 column menu, A-17): the trips that are in an active automation's stage or carry its tag but never had it
// (GET /workflows/coverage). Preview = the dry run of the apply (how many can start it now, the rest say why not in the
// automation's own preview); Run = the apply for those trips (owner, agent; at most 100 per press, the API's limit),
// started after the Undo window like any apply. ``stage`` limits the list to one pipeline stage's rules.
import { useEffect, useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import { canApply, workflowErrorKey } from "@/lib/workflows";
import type { Coverage, CoverageRule } from "@/lib/tags";

const MAX_APPLY = 100;

function Row({ rule, role, lang, onRan }: { rule: CoverageRule; role: Role; lang: Lang; onRan: () => void }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [eligible, setEligible] = useState<string[] | null>(null);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const ids = rule.trip_ids.slice(0, MAX_APPLY);

  async function apply(dryRun: boolean) {
    setBusy(true);
    const res = await bffSend<{ eligible: string[]; started?: number }>("POST", `workflows/${rule.workflow_rule_id}/apply`, {
      trip_ids: dryRun ? ids : (eligible ?? []),
      dry_run: dryRun,
    });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(workflowErrorKey(res.detail)) });
      return;
    }
    if (dryRun) {
      setEligible(res.data.eligible);
      setNotice({ tone: "success", text: fmt(tr("tag.coverage.eligible"), { n: res.data.eligible.length }) });
      return;
    }
    setEligible(null);
    setNotice({ tone: "success", text: fmt(tr("tag.coverage.started"), { n: res.data.started ?? 0 }) });
    window.setTimeout(onRan, 25_000);
  }

  return (
    <li data-testid="coverage-rule" data-count={rule.missing_count} style={{ display: "grid", gap: 4 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <span style={{ fontSize: 13, overflowWrap: "anywhere" }}>{fmt(tr("tag.coverage.missing"), { n: rule.missing_count, rule: rule.name })}</span>
        {canApply(role) && (
          <>
            <Button size="sm" variant="secondary" disabled={busy || !hydrated} onClick={() => void apply(true)}>
              {tr("tag.coverage.check")}
            </Button>
            <Button size="sm" variant="primary" disabled={busy || !hydrated || !eligible || eligible.length === 0} onClick={() => void apply(false)}>
              {fmt(tr("tag.coverage.run"), { n: eligible?.length ?? 0 })}
            </Button>
          </>
        )}
      </div>
      {notice && (
        <span role="status" style={{ fontSize: 12.5, color: notice.tone === "danger" ? K.danger : K.success }}>
          {notice.text}
        </span>
      )}
    </li>
  );
}

export function CoveragePanel({ role, lang, stage, initial }: { role: Role; lang: Lang; stage?: string; initial?: Coverage | null }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [view, setView] = useState<Coverage | null>(initial ?? null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (initial && tick === 0) return;
    let stopped = false;
    void (async () => {
      const res = await bffSend<Coverage>("GET", "workflows/coverage");
      if (!stopped && res.ok) setView(res.data);
    })();
    return () => {
      stopped = true;
    };
  }, [initial, tick]);

  const rules = (view?.rules ?? []).filter((r) => !stage || r.stage === stage);
  return (
    <Panel title={tr("tag.coverage.title")} testId="workflow-coverage">
      <p style={{ margin: "0 0 8px", fontSize: 12.5, color: K.muted }}>{tr("tag.coverage.help")}</p>
      {view !== null && rules.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("tag.coverage.none")}</p>}
      {rules.length > 0 && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
          {rules.map((r) => (
            <Row key={r.workflow_rule_id} rule={r} role={role} lang={lang} onRan={() => setTick((n) => n + 1)} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
