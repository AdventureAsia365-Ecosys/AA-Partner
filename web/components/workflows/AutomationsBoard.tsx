"use client";
// web/components/workflows/AutomationsBoard.tsx — A-17 Automations (card CRM-29a, AA-959; design v3 M10, R9, "owner
// edits; agent applies"): the agency's rules (what starts each, its conditions and steps, on / paused / archived, how
// many runs are going and finished, a preview of how many live trips it applies to now) and the AA library of
// ready-made automations. The owner adds from the library (a template that sends a message starts paused until its
// message template is picked), builds, edits, switches on / pauses and archives; every other role reads.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, ConfirmModal, EmptyState, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { RuleEditor } from "@/components/workflows/RuleEditor";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { ChannelTemplate } from "@/lib/messaging";
import type { Member } from "@/lib/pipeline";
import type { MessageTemplate } from "@/lib/textProposal";
import { stepText, templateDescription, templateName, triggerText, workflowErrorKey, type Rule, type WorkflowOverview } from "@/lib/workflows";

const STATUS_TONE = { active: "success", paused: "warning", archived: "neutral" } as const;

export function AutomationsBoard({
  view,
  templates,
  textTemplates,
  members,
  lang,
}: {
  view: WorkflowOverview;
  templates: MessageTemplate[];
  textTemplates: ChannelTemplate[];
  members: Member[];
  lang: Lang;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  const [archiving, setArchiving] = useState<Rule | null>(null);
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const owner = view.can_edit;

  async function run(method: "POST" | "PATCH", path: string, body?: unknown, ok?: string): Promise<boolean> {
    setBusy(true);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(workflowErrorKey(res.detail)) });
      return false;
    }
    setNotice(ok ? { tone: "success", text: ok } : null);
    router.refresh();
    return true;
  }

  async function preview(rule: Rule) {
    const res = await bffSend<{ subject: string; count: number | null }>("GET", `workflows/${rule.workflow_rule_id}/preview`);
    if (!res.ok) return;
    const text = res.data.subject === "client" ? tr("wf.previewClient") : fmt(tr("wf.previewCount"), { n: res.data.count ?? 0 });
    setPreviews({ ...previews, [rule.workflow_rule_id]: text });
  }

  return (
    <div data-testid="automations" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
      {!owner && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("wf.ownerOnly")}</p>}
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
          {notice.text}
        </p>
      )}
      <Panel title={tr("wf.rules")} testId="wf-rules">
        {owner && (
          <div style={{ marginBottom: 10 }} data-testid="wf-new">
            <Button size="sm" variant="primary" disabled={!hydrated} onClick={() => setEditing("new")}>
              {tr("wf.new")}
            </Button>
          </div>
        )}
        {view.rules.length === 0 ? (
          <EmptyState title={tr("wf.rules")} description={tr("wf.rulesEmpty")} />
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 12 }}>
            {view.rules.map((rule) => (
              <li key={rule.workflow_rule_id} data-testid="wf-rule" data-status={rule.status} style={{ display: "grid", gap: 6, paddingTop: 10, borderTop: `1px solid ${K.line}` }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 14, overflowWrap: "anywhere" }}>{rule.name}</strong>
                  <Badge tone={STATUS_TONE[rule.status]}>{tr(`wf.status.${rule.status}`)}</Badge>
                  {rule.template_key && <Badge tone="info">{tr("wf.library")}</Badge>}
                </div>
                <span style={{ fontSize: 13, color: K.ink }}>{triggerText(lang, rule.trigger)}</span>
                <ol style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: K.muted, display: "grid", gap: 2 }}>
                  {rule.steps.map((step, n) => (
                    <li key={n} style={{ overflowWrap: "anywhere" }}>
                      {stepText(lang, step)}
                      {rule.templates_missing.includes(n) && <span style={{ color: K.danger }}> · {tr("wf.needsTemplate")}</span>}
                    </li>
                  ))}
                </ol>
                <span style={{ fontSize: 12, color: K.muted }}>
                  {fmt(tr("wf.runs"), { active: rule.active_runs ?? 0, finished: rule.finished_runs ?? 0 })}
                  {previews[rule.workflow_rule_id] ? ` · ${previews[rule.workflow_rule_id]}` : ""}
                </span>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => void preview(rule)}>
                    {tr("wf.preview")}
                  </Button>
                  {owner && rule.status !== "archived" && (
                    <>
                      <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => setEditing(rule)}>
                        {tr("wf.edit")}
                      </Button>
                      {rule.status === "paused" ? (
                        <span data-testid="wf-activate">
                          <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => void run("POST", `workflows/${rule.workflow_rule_id}/status`, { status: "active" })}>
                            {tr("wf.activate")}
                          </Button>
                        </span>
                      ) : (
                        <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => void run("POST", `workflows/${rule.workflow_rule_id}/status`, { status: "paused" })}>
                          {tr("wf.pause")}
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => setArchiving(rule)}>
                        {tr("wf.archive")}
                      </Button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={tr("wf.library")} testId="wf-library">
        <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{tr("wf.libraryHint")}</p>
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 12 }}>
          {view.library.map((tpl) => (
            <li key={tpl.key} data-testid="wf-template" data-key={tpl.key} style={{ display: "grid", gap: 4, paddingTop: 10, borderTop: `1px solid ${K.line}` }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "space-between" }}>
                <strong style={{ fontSize: 13.5 }}>{templateName(lang, tpl)}</strong>
                {tpl.installed ? (
                  <Badge tone="success">{tr("wf.installed")}</Badge>
                ) : (
                  owner && (
                    <span data-testid="wf-install">
                      <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => void run("POST", "workflows/install", { template_key: tpl.key }, tr("wf.added"))}>
                        {tr("wf.install")}
                      </Button>
                    </span>
                  )
                )}
              </div>
              <span style={{ fontSize: 12.5, color: K.muted }}>{templateDescription(lang, tpl.key)}</span>
              <span style={{ fontSize: 12, color: K.muted }}>
                {triggerText(lang, tpl.trigger)}
                {tpl.needs_templates ? ` · ${tr("wf.needsTemplate")}` : ""}
              </span>
            </li>
          ))}
        </ul>
      </Panel>
      {editing && (
        <RuleEditor
          lang={lang}
          options={view.options}
          templates={templates}
          textTemplates={textTemplates}
          members={members}
          rule={editing === "new" ? undefined : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            setNotice({ tone: "success", text: tr("wf.saved") });
            router.refresh();
          }}
        />
      )}
      <ConfirmModal
        open={archiving !== null}
        onClose={() => setArchiving(null)}
        onConfirm={async () => {
          if (archiving && (await run("POST", `workflows/${archiving.workflow_rule_id}/status`, { status: "archived" }))) setArchiving(null);
        }}
        title={tr("wf.archive")}
        body={tr("wf.archiveConfirm")}
        confirmLabel={tr("wf.archive")}
        cancelLabel={tr("wf.cancel")}
        destructive
        busy={busy}
        testId="wf-archive-confirm"
      />
    </div>
  );
}
