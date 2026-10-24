"use client";
// web/components/intake/PlanningForm.tsx — the Stage 2 planning form (C-02, AA-943 / CRM-15; spec §5 "Stage 2", R31),
// paged. One page at a time from the API's `stage2.v1` definition (lib/planningForm.ts): travel style, interests,
// planning, the diet-and-mobility consent, then the diet and mobility page only when the consent is ticked. The
// altitude and trek questions show only for an adventure trip. Every question is optional; the body carries the
// visible answers only (the server drops hidden ones again and counts them). A refused answer sends the person back to
// the page of the first refused field.
//
// Two uses: the client's signed link (/c/s/<token>, mode "client") and the agent's "Fill in from call" on the trip
// page (mode "agent": the consent is recorded as given on the call). Server-rendered on the client page: the buttons
// stay disabled until hydration and the form says `data-hydrated="true"` once its handlers are attached (AA-936).
import { useEffect, useMemo, useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import type { MessageKey } from "@/lib/i18n";
import { fmt } from "@/lib/i18n";
import {
  answerBody,
  firstErrorPage,
  knownAnswers,
  OPTION_PREFIX,
  toggleCode,
  visiblePages,
  type Answers,
  type PlanField,
  type PlanPage,
} from "@/lib/planningForm";

type Dict = Partial<Record<MessageKey, string>>;
export type SubmitResult = { ok: true; data?: unknown } | { ok: false; status: number; detail: string; body?: unknown };

const input: React.CSSProperties = {
  font: "inherit",
  fontSize: 14,
  padding: "8px 10px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  minWidth: 0,
  width: "100%",
  boxSizing: "border-box",
};

export function PlanningForm({
  pages,
  tripStyle,
  initial,
  agency,
  mode,
  dict,
  onSubmit,
  onDone,
}: {
  pages: PlanPage[];
  tripStyle: string | null;
  initial?: Record<string, unknown> | null;
  agency: string;
  mode: "client" | "agent";
  dict: Dict;
  onSubmit: (body: Answers) => Promise<SubmitResult>;
  onDone?: (data: unknown) => void;
}) {
  const t = (k: MessageKey) => dict[k] ?? k;
  const code = (prefix: string | undefined, c: string) => (prefix ? (dict[`${prefix}.${c}` as MessageKey] ?? c) : c);
  const submissionId = useMemo(() => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : ""), []);
  const [answers, setAnswers] = useState<Answers>(() => knownAnswers(pages, initial));
  const [index, setIndex] = useState(0);
  const [bad, setBad] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const hydrated = useHydrated();

  const visible = visiblePages(pages, tripStyle, answers);
  const last = visible.length - 1;
  const at = Math.min(index, Math.max(last, 0));
  const page = visible[at];

  // A page can disappear (the consent unticked): keep the index on a page that exists.
  useEffect(() => {
    if (index > last && last >= 0) setIndex(last);
  }, [index, last]);

  function set(name: string, value: unknown) {
    setAnswers((a) => ({ ...a, [name]: value }));
    setBad((b) => b.filter((n) => n !== name));
  }

  async function submit() {
    setSending(true);
    setError(null);
    const res = await onSubmit(answerBody(pages, tripStyle, answers, submissionId));
    setSending(false);
    if (res.ok) {
      setSent(true);
      onDone?.(res.data);
      return;
    }
    const body = (res.body ?? {}) as { fields?: unknown };
    if (res.detail === "invalid_submission") {
      const fields = Array.isArray(body.fields) ? body.fields.filter((f): f is string => typeof f === "string") : [];
      setBad(fields);
      const jump = firstErrorPage(visible, fields);
      if (jump !== null) setIndex(jump);
      setError(t("plan.invalid"));
    } else if (res.detail === "health_consent_required") {
      setBad(["health_consent"]);
      const jump = firstErrorPage(visible, ["health_consent"]);
      if (jump !== null) setIndex(jump);
      setError(t("plan.healthConsent"));
    } else if (res.status === 429) setError(t("plan.rateLimited"));
    else if (res.status === 404) setError(t("plan.notFound"));
    else setError(t("plan.error"));
  }

  if (sent && mode === "client") {
    return (
      <div data-testid="plan-thanks" role="status">
        <h2 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>{t("plan.thanks")}</h2>
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>{fmt(t("plan.thanksBody"), { agency })}</p>
      </div>
    );
  }
  if (!page) return null;

  return (
    <form
      noValidate
      data-testid="plan-form"
      data-hydrated={hydrated ? "true" : undefined}
      data-page={page.key}
      data-pages={visible.map((p) => p.key).join(" ")}
      onSubmit={(e) => {
        e.preventDefault();
        if (at < last) setIndex(at + 1);
        else void submit();
      }}
      style={{ display: "grid", gap: 14 }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}>
        <h2 style={{ margin: 0, fontSize: 16, color: K.ink }} data-testid="plan-page-title">
          {t(`plan.page.${page.key}` as MessageKey)}
        </h2>
        <span style={{ fontSize: 12, color: K.muted }}>{fmt(t("plan.step"), { n: at + 1, total: visible.length })}</span>
      </div>
      {page.fields.map((f) => (
        <FieldInput key={f.name} field={f} value={answers[f.name]} bad={bad.includes(f.name)} set={set} t={t} code={code} agency={agency} mode={mode} />
      ))}
      {error && (
        <p role="alert" data-testid="plan-error" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
        <span data-testid="plan-back">
          {at > 0 && (
            <Button variant="ghost" disabled={!hydrated || sending} onClick={() => setIndex(at - 1)}>
              {t("plan.back")}
            </Button>
          )}
        </span>
        <span data-testid={at < last ? "plan-next" : "plan-submit"}>
          <Button type="submit" variant="primary" disabled={!hydrated || sending}>
            {at < last ? t("plan.next") : sending ? t("plan.sending") : t(mode === "client" ? "plan.submit" : "plan.save")}
          </Button>
        </span>
      </div>
    </form>
  );
}

function FieldInput({
  field,
  value,
  bad,
  set,
  t,
  code,
  agency,
  mode,
}: {
  field: PlanField;
  value: unknown;
  bad: boolean;
  set: (name: string, value: unknown) => void;
  t: (k: MessageKey) => string;
  code: (prefix: string | undefined, c: string) => string;
  agency: string;
  mode: "client" | "agent";
}) {
  const label = t(`plan.field.${field.name}` as MessageKey);
  const prefix = OPTION_PREFIX[field.name];
  const outline = bad ? { outline: `1px solid ${K.danger}`, borderRadius: 8 } : {};
  const id = `plan-${field.name}`;
  if (field.type === "consent") {
    return (
      <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13.5, color: bad ? K.danger : K.ink2 }} data-testid={id}>
        <input type="checkbox" name={field.name} checked={value === true} onChange={(e) => set(field.name, e.target.checked)} style={{ marginTop: 3 }} />
        <span>{mode === "client" ? fmt(t("plan.consent"), { agency }) : t("plan.consentAgent")}</span>
      </label>
    );
  }
  if (field.type === "select") {
    return (
      <label style={{ display: "grid", gap: 4, fontSize: 13, color: K.ink2 }}>
        {label}
        <select name={field.name} value={typeof value === "string" ? value : ""} onChange={(e) => set(field.name, e.target.value || null)} style={{ ...input, ...(bad ? { borderColor: K.danger } : {}) }} data-testid={id}>
          <option value="">{t("plan.choose")}</option>
          {(field.options ?? []).map((o) => (
            <option key={o} value={o}>
              {code(prefix, o)}
            </option>
          ))}
        </select>
      </label>
    );
  }
  if (field.type === "boolean") {
    return (
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 6, ...outline }} data-testid={id}>
        <legend style={{ fontSize: 13, color: K.ink2, marginBottom: 6 }}>{label}</legend>
        <span style={{ display: "flex", gap: 16, fontSize: 13.5 }}>
          {[true, false].map((v) => (
            <label key={String(v)} style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="radio" name={field.name} checked={value === v} onChange={() => set(field.name, v)} />
              {t(v ? "common.yes" : "common.no")}
            </label>
          ))}
        </span>
      </fieldset>
    );
  }
  if (field.type === "multi_select") {
    const chosen = Array.isArray(value) ? (value as string[]) : [];
    return (
      <div role="group" aria-label={label} style={{ display: "grid", gap: 6 }} data-testid={id}>
        <span style={{ fontSize: 13, color: K.ink2 }}>
          {label}
          {field.max_items ? <span style={{ color: K.muted, fontSize: 12 }}> · {fmt(t("plan.maxItems"), { n: field.max_items })}</span> : null}
        </span>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, ...outline }}>
          {(field.options ?? []).map((o) => {
            const on = chosen.includes(o);
            return (
              <button
                key={o}
                type="button"
                aria-pressed={on}
                data-testid={`${id}-${o}`}
                onClick={() => set(field.name, toggleCode(chosen, o, field.max_items))}
                style={{
                  font: "inherit",
                  fontSize: 13,
                  padding: "6px 12px",
                  borderRadius: 999,
                  cursor: "pointer",
                  border: `1px solid ${on ? K.accent : K.line2}`,
                  background: on ? K.accentTint : K.card,
                  color: on ? K.accentDeep : K.ink2,
                }}
              >
                {code(prefix, o)}
              </button>
            );
          })}
        </div>
      </div>
    );
  }
  return (
    <label style={{ display: "grid", gap: 4, fontSize: 13, color: K.ink2 }}>
      {label}
      <textarea
        name={field.name}
        rows={4}
        maxLength={field.max_length ?? 1000}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => set(field.name, e.target.value)}
        style={{ ...input, resize: "vertical", ...(bad ? { borderColor: K.danger } : {}) }}
        data-testid={id}
      />
    </label>
  );
}
