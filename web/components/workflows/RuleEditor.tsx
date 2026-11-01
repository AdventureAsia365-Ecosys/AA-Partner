"use client";
// web/components/workflows/RuleEditor.tsx — the automation editor of A-17 (card CRM-29a, AA-959; design v3 M10): name,
// what starts it (a stage, a trip event, a date of the trip, a client's special date, or by hand = a task list), the
// conditions (stages, booking details missing, children, AA services) and the steps in order (create a task, send an
// e-mail from the agency's mailbox, send an approved SMS / WhatsApp template, set a field, wait). Owner only (the API
// checks it too). The API validates everything again and answers with the field that is wrong (422).
import { useState } from "react";
import { Button, Checkbox, Field, Input, K, Modal, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { memberLabel, type Member } from "@/lib/pipeline";
import type { ChannelTemplate } from "@/lib/messaging";
import type { MessageTemplate } from "@/lib/textProposal";
import { STAGES, actionsFor, isGreeting, workflowErrorKey, type Rule, type Step, type Trigger, type WorkflowOptions } from "@/lib/workflows";

export interface EditorDraft {
  name: string;
  trigger: Trigger;
  conditions: Record<string, unknown>;
  steps: Step[];
}

export function draftOf(rule?: Pick<Rule, "name" | "trigger" | "conditions" | "steps">): EditorDraft {
  if (rule) return { name: rule.name, trigger: { ...rule.trigger }, conditions: { ...rule.conditions }, steps: rule.steps.map((s) => ({ ...s })) };
  return {
    name: "",
    trigger: { kind: "stage_enter", value: "booked", offset_days: 0 },
    conditions: {},
    steps: [{ action: "create_task", kind: "custom", title: "", due: { anchor: "created", offset_days: 0 }, priority: "normal", assignee: "owner" }],
  };
}

function blankStep(action: Step["action"], greeting = false): Step {
  switch (action) {
    case "create_task":
      return { action, kind: "custom", title: "", due: { anchor: "created", offset_days: 0 }, priority: "normal", assignee: "owner" };
    case "send_email":
      return { action, message_template_id: null, purpose: greeting ? "marketing" : "transactional" };
    case "send_text":
      return { action, channel: "whatsapp", content_sid: null, variables: {} };
    case "set_field":
      return { action, field: "waiting_on", value: "client" };
    default:
      return { action: "wait", days: 1, hours: 0 };
  }
}

const TRIGGER_VALUES = (options: WorkflowOptions, kind: Trigger["kind"]): string[] =>
  kind === "stage_enter" ? [...STAGES] : kind === "event" ? options.events : kind === "date_anchor" ? options.date_anchors : kind === "special_date" ? options.special_dates : [];

function num(value: string, fallback = 0): number {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) ? n : fallback;
}

/** The body the API takes (POST /workflows, PATCH /workflows/{id}): the draft as it is, empty titles trimmed; a
 * birthday or anniversary rule's messages are marketing (the API refuses transactional there). */
export function payloadOf(draft: EditorDraft): Record<string, unknown> {
  const trigger: Record<string, unknown> = { kind: draft.trigger.kind, value: draft.trigger.kind === "manual" ? null : draft.trigger.value };
  if (draft.trigger.kind === "date_anchor" || draft.trigger.kind === "special_date") trigger.offset_days = draft.trigger.offset_days;
  const conditions = draft.trigger.kind === "special_date" ? {} : Object.fromEntries(Object.entries(draft.conditions).filter(([, v]) => v !== undefined && !(Array.isArray(v) && v.length === 0)));
  const greeting = isGreeting(draft.trigger);
  const steps = greeting ? draft.steps.map((s) => (s.action === "send_email" || s.action === "send_text" ? { ...s, purpose: "marketing" } : s)) : draft.steps;
  return { name: draft.name.trim(), trigger, conditions, steps };
}

function StepFields({
  step,
  n,
  lang,
  options,
  templates,
  textTemplates,
  members,
  client,
  greeting,
  onChange,
}: {
  step: Step;
  n: number;
  lang: Lang;
  options: WorkflowOptions;
  templates: MessageTemplate[];
  textTemplates: ChannelTemplate[];
  members: Member[];
  client: boolean;
  greeting: boolean;
  onChange: (next: Step) => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const set = (patch: Record<string, unknown>) => onChange({ ...step, ...patch } as Step);
  if (step.action === "create_task") {
    const due = (step.due as { anchor: string; offset_days: number }) ?? { anchor: "created", offset_days: 0 };
    return (
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
        <Field label={tr("wf.taskKind")}>
          <Select value={String(step.kind)} onChange={(e) => set({ kind: e.target.value, title: e.target.value === "custom" ? "" : undefined })} options={[{ value: "custom", label: tr("wf.taskCustom") }, ...options.task_kinds.map((k) => ({ value: k, label: tr(`task.${k}`) }))]} />
        </Field>
        {step.kind === "custom" && (
          <Field label={tr("wf.taskTitle")} required>
            <Input value={String(step.title ?? "")} maxLength={120} onChange={(e) => set({ title: e.target.value })} data-step={n} />
          </Field>
        )}
        <Field label={tr("wf.dueAnchor")}>
          <Select value={due.anchor} onChange={(e) => set({ due: { ...due, anchor: e.target.value } })} options={(client ? ["created"] : options.due_anchors).map((a) => ({ value: a, label: tr(`wf.dueAnchor.${a}`) }))} />
        </Field>
        <Field label={tr("wf.dueDays")}>
          <Input type="number" min={-365} max={365} value={String(due.offset_days ?? 0)} onChange={(e) => set({ due: { ...due, offset_days: num(e.target.value) } })} />
        </Field>
        <Field label={tr("tasks.priority")}>
          <Select value={String(step.priority ?? "normal")} onChange={(e) => set({ priority: e.target.value })} options={[{ value: "normal", label: tr("tasks.priority.normal") }, { value: "urgent", label: tr("tasks.priority.urgent") }]} />
        </Field>
        <Field label={tr("tasks.assignee")}>
          <Select value={String(step.assignee ?? "owner")} onChange={(e) => set({ assignee: e.target.value })} options={[{ value: "owner", label: tr("wf.field.owner") }, ...members.map((m) => ({ value: `member:${m.member_id}`, label: memberLabel(lang, m) }))]} />
        </Field>
      </div>
    );
  }
  if (step.action === "send_email") {
    return (
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
        <Field label={tr("wf.template")} required>
          <Select value={String(step.message_template_id ?? "")} onChange={(e) => set({ message_template_id: e.target.value || null })} placeholder={tr("wf.templatePick")} options={templates.map((tp) => ({ value: tp.message_template_id, label: `${tp.title} · ${tp.locale.toUpperCase()}` }))} />
        </Field>
        <Field label={tr("wf.purpose")} hint={greeting ? tr("wf.purpose.greeting") : undefined}>
          <Select value={greeting ? "marketing" : String(step.purpose ?? "transactional")} disabled={greeting} onChange={(e) => set({ purpose: e.target.value })} options={["transactional", "marketing"].map((p) => ({ value: p, label: tr(`wf.purpose.${p}`) }))} data-testid="wf-purpose" />
        </Field>
      </div>
    );
  }
  if (step.action === "send_text") {
    const chosen = textTemplates.find((x) => x.content_sid === step.content_sid);
    const variables = (step.variables as Record<string, string>) ?? {};
    return (
      <div style={{ display: "grid", gap: 8 }}>
        <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
          <Field label={tr("wf.channel")}>
            <Select value={String(step.channel)} onChange={(e) => set({ channel: e.target.value })} options={["whatsapp", "sms"].map((c) => ({ value: c, label: tr(`msg.channel.${c}`) }))} />
          </Field>
          <Field label={tr("wf.textTemplate")} required hint={textTemplates.length === 0 ? tr("wf.textTemplateNone") : undefined}>
            <Select value={String(step.content_sid ?? "")} onChange={(e) => set({ content_sid: e.target.value || null, variables: {} })} placeholder={tr("wf.templatePick")} options={textTemplates.map((x) => ({ value: x.content_sid, label: `${x.name} · ${x.language}` }))} />
          </Field>
        </div>
        {chosen && (chosen.variables ?? []).length > 0 && (
          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
            {(chosen.variables ?? []).map((v) => (
              <Field key={v} label={t(lang, "wf.variable").replace("{name}", v)}>
                <Select value={variables[v] ?? ""} onChange={(e) => set({ variables: { ...variables, [v]: e.target.value } })} placeholder="—" options={options.merge_fields.map((f) => ({ value: f, label: `{{${f}}}` }))} />
              </Field>
            ))}
          </div>
        )}
      </div>
    );
  }
  if (step.action === "set_field") {
    return (
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
        <Field label={tr("wf.field")}>
          <Select value={String(step.field)} onChange={(e) => set({ field: e.target.value, value: e.target.value === "owner" ? (members[0] ? `member:${members[0].member_id}` : "") : "client" })} options={["waiting_on", "owner"].map((f) => ({ value: f, label: tr(`wf.field.${f}`) }))} />
        </Field>
        <Field label={tr("wf.value")}>
          {step.field === "owner" ? (
            <Select value={String(step.value)} onChange={(e) => set({ value: e.target.value })} options={members.map((m) => ({ value: `member:${m.member_id}`, label: memberLabel(lang, m) }))} />
          ) : (
            <Select value={String(step.value)} onChange={(e) => set({ value: e.target.value })} options={options.waiting_on.map((w) => ({ value: w, label: tr(`waiting.${w}`) }))} />
          )}
        </Field>
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
      <Field label={tr("wf.waitDays")}>
        <Input type="number" min={0} max={365} value={String(step.days ?? 0)} onChange={(e) => set({ days: num(e.target.value), until: undefined })} />
      </Field>
      <Field label={tr("wf.waitHours")}>
        <Input type="number" min={0} max={23} value={String(step.hours ?? 0)} onChange={(e) => set({ hours: num(e.target.value), until: undefined })} />
      </Field>
    </div>
  );
}

export function RuleEditor({
  lang,
  options,
  templates,
  textTemplates,
  members,
  rule,
  onClose,
  onSaved,
}: {
  lang: Lang;
  options: WorkflowOptions;
  templates: MessageTemplate[];
  textTemplates: ChannelTemplate[];
  members: Member[];
  rule?: Rule;
  onClose: () => void;
  onSaved: () => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [draft, setDraft] = useState<EditorDraft>(() => draftOf(rule));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const kind = draft.trigger.kind;
  const client = kind === "special_date";
  const greeting = isGreeting(draft.trigger);
  const actions = actionsFor(draft.trigger);
  const values = TRIGGER_VALUES(options, kind);
  const stages = (draft.conditions.stages as string[] | undefined) ?? [];
  const setCond = (key: string, value: unknown) => setDraft({ ...draft, conditions: { ...draft.conditions, [key]: value } });

  async function save() {
    setBusy(true);
    const body = payloadOf(draft);
    const res = rule ? await bffSend("PATCH", `workflows/${rule.workflow_rule_id}`, { ...body, version: rule.version }) : await bffSend("POST", "workflows", { ...body, status: "paused" });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) {
        const field = (res.body as { field?: string } | undefined)?.field;
        setError(`${tr(workflowErrorKey(res.detail))}${field ? ` (${field})` : ""}`);
      }
      return;
    }
    onSaved();
  }

  return (
    <Modal open onClose={onClose} title={rule ? tr("wf.edit") : tr("wf.new")} width={760} testId="wf-editor">
      <div style={{ display: "grid", gap: 14 }}>
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("wf.noClientData")}</p>
        <Field label={tr("wf.name")} required>
          <Input value={draft.name} maxLength={120} onChange={(e) => setDraft({ ...draft, name: e.target.value })} data-testid="wf-name" />
        </Field>
        <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
          <Field label={tr("wf.triggerKind")}>
            <Select
              value={kind}
              onChange={(e) => {
                const next = e.target.value as Trigger["kind"];
                const first = TRIGGER_VALUES(options, next)[0] ?? null;
                setDraft({ ...draft, trigger: { kind: next, value: next === "manual" ? null : first, offset_days: 0 }, conditions: next === "special_date" ? {} : draft.conditions });
              }}
              options={options.trigger_kinds.map((k) => ({ value: k, label: tr(`wf.trigger.kind.${k}`) }))}
            />
          </Field>
          {kind !== "manual" && (
            <Field label={tr("wf.trigger.value")}>
              <Select
                value={String(draft.trigger.value ?? "")}
                onChange={(e) => setDraft({ ...draft, trigger: { ...draft.trigger, value: e.target.value } })}
                options={values.map((v) => ({ value: v, label: kind === "stage_enter" ? tr(`stage.${v}`) : kind === "event" ? tr(`wf.event.${v}`) : tr(`wf.anchor.${v}`) }))}
              />
            </Field>
          )}
          {(kind === "date_anchor" || kind === "special_date") && (
            <Field label={tr("wf.trigger.offset")}>
              <Input type="number" min={-400} max={400} value={String(draft.trigger.offset_days)} onChange={(e) => setDraft({ ...draft, trigger: { ...draft.trigger, offset_days: num(e.target.value) } })} />
            </Field>
          )}
        </div>
        {!client && (
          <Field label={tr("wf.conditions")} as="div">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
              {STAGES.map((s) => (
                <Checkbox
                  key={s}
                  label={tr(`stage.${s}`)}
                  checked={stages.includes(s)}
                  onChange={(e) => setCond("stages", e.target.checked ? [...stages, s] : stages.filter((x) => x !== s))}
                />
              ))}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 6 }}>
              {(["stage3_incomplete", "has_children", "has_aa_lines"] as const).map((key) => (
                <Checkbox key={key} label={tr(`wf.cond.${key}`)} checked={draft.conditions[key] === true} onChange={(e) => setCond(key, e.target.checked ? true : undefined)} />
              ))}
            </div>
          </Field>
        )}
        <div style={{ display: "grid", gap: 10 }}>
          <strong style={{ fontSize: 13 }}>{tr("wf.steps")}</strong>
          {draft.steps.map((step, n) => (
            <div key={n} data-testid="wf-step" style={{ display: "grid", gap: 8, padding: 10, border: `1px solid ${K.line}`, borderRadius: 8 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
                <Field label={`${n + 1}. ${tr("wf.action")}`} style={{ minWidth: 220 }}>
                  <Select
                    value={step.action}
                    onChange={(e) => setDraft({ ...draft, steps: draft.steps.map((s, i) => (i === n ? blankStep(e.target.value as Step["action"], greeting) : s)) })}
                    options={(actions.includes(step.action) ? actions : [...actions, step.action]).map((a) => ({ value: a, label: tr(`wf.action.${a}`) }))}
                  />
                </Field>
                {draft.steps.length > 1 && (
                  <Button size="sm" variant="ghost" onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, i) => i !== n) })}>
                    {tr("wf.removeStep")}
                  </Button>
                )}
              </div>
              <StepFields
                step={step}
                n={n}
                lang={lang}
                options={options}
                templates={templates}
                textTemplates={textTemplates}
                members={members}
                client={client}
                greeting={greeting}
                onChange={(next) => setDraft({ ...draft, steps: draft.steps.map((s, i) => (i === n ? next : s)) })}
              />
            </div>
          ))}
          {draft.steps.length < 20 && (
            <div>
              <Button size="sm" variant="dashed" onClick={() => setDraft({ ...draft, steps: [...draft.steps, blankStep("create_task")] })}>
                {tr("wf.addStep")}
              </Button>
            </div>
          )}
        </div>
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
            {error}
          </p>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <span data-testid="wf-save">
            <Button variant="primary" disabled={!hydrated || busy || !draft.name.trim()} onClick={() => void save()}>
              {tr("wf.save")}
            </Button>
          </span>
          <Button variant="ghost" onClick={onClose}>
            {tr("wf.cancel")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
