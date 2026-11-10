"use client";
// web/components/settings/ScheduleTemplateForm.tsx — Settings > Payments, "Payment schedule template" (AA-1067,
// crm-050): the lines a new proposal starts with: the deposit (a share at acceptance), instalments (a share N days
// before departure) and the balance (the rest N days before). Agents change them on each proposal before publishing.
// "Use the Adventure Asia default" clears the template (30 % now, 40 % at T-60, the rest at T-30). Owner only.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, Input, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { paymentsErrorKey, templateBody, templateLineText, templateRows, type ScheduleKind, type ScheduleSettings } from "@/lib/retailPayments";

type Row = { kind: ScheduleKind; percent: string; days: string };

export function ScheduleTemplateForm({ view, lang }: { view: ScheduleSettings; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [rows, setRows] = useState<Row[]>(() => templateRows(view.template));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const hasBalance = rows.some((r) => r.kind === "balance");

  function set(i: number, patch: Partial<Row>) {
    setRows(rows.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  }

  function addInstalment() {
    const at = rows.findIndex((r) => r.kind === "balance");
    const line: Row = { kind: "instalment", percent: "10", days: "45" };
    setRows(at < 0 ? [...rows, line] : [...rows.slice(0, at), line, ...rows.slice(at)]);
  }

  async function put(template: unknown) {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<ScheduleSettings>("PUT", "settings/payment-schedule", { template });
    setBusy(false);
    if (!res.ok) {
      const key = paymentsErrorKey(res.detail);
      setMessage({ tone: "danger", text: key ? tr(key) : tr("schedule.invalid") });
      return;
    }
    setRows(templateRows(res.data.template));
    setMessage({ tone: "success", text: tr("schedule.settings.saved") });
    router.refresh();
  }

  async function save() {
    const body = templateBody(rows);
    if (!body) {
      setMessage({ tone: "danger", text: tr("schedule.invalid") });
      return;
    }
    await put(body);
  }

  return (
    <Panel title={tr("schedule.settings.title")} testId="schedule-template">
      <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>{tr("schedule.settings.hint")}</p>
      {view.is_default && (
        <p style={{ margin: "0 0 8px", fontSize: 13 }} data-testid="schedule-template-default">
          {tr("schedule.settings.isDefault")}
        </p>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
        style={{ display: "grid", gap: 8 }}
      >
        {rows.map((r, i) => {
          const n = rows.slice(0, i + 1).filter((x) => x.kind === "instalment").length;
          const label = r.kind === "instalment" ? fmt(tr("schedule.kind.instalment"), { n }) : tr(`schedule.kind.${r.kind}`);
          return (
            <div key={`${r.kind}-${i}`} data-testid="schedule-template-line" data-kind={r.kind} style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
              <span style={{ fontSize: 13, minWidth: 110 }}>{label}</span>
              {r.kind !== "balance" && (
                <Field label={tr("schedule.percent")}>
                  <Input size="sm" inputMode="decimal" value={r.percent} onChange={(e) => set(i, { percent: e.target.value })} style={{ width: 90 }} />
                </Field>
              )}
              {r.kind === "deposit" ? (
                <span style={{ fontSize: 12.5, color: K.muted }}>{tr("schedule.atAccept")}</span>
              ) : (
                <Field label={tr("schedule.settings.days")}>
                  <Input size="sm" inputMode="numeric" value={r.days} onChange={(e) => set(i, { days: e.target.value })} style={{ width: 90 }} />
                </Field>
              )}
              {r.kind !== "deposit" && (
                <Button type="button" size="sm" variant="ghost" onClick={() => setRows(rows.filter((_, n) => n !== i))}>
                  {tr("schedule.remove")}
                </Button>
              )}
            </div>
          );
        })}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button type="button" size="sm" variant="secondary" onClick={addInstalment} disabled={rows.filter((r) => r.kind === "instalment").length >= 10}>
            {tr("schedule.add")}
          </Button>
          {!hasBalance && (
            <Button type="button" size="sm" variant="secondary" onClick={() => setRows([...rows, { kind: "balance", percent: "", days: "30" }])}>
              {tr("schedule.settings.addBalance")}
            </Button>
          )}
          <span data-testid="schedule-template-save">
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("schedule.settings.save")}
            </Button>
          </span>
          <Button type="button" size="sm" variant="ghost" disabled={!hydrated || busy || view.is_default} onClick={() => void put(null)}>
            {tr("schedule.settings.useDefault")}
          </Button>
        </div>
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.muted }}>
          {view.aa_default.map((l) => (
            <li key={`${l.kind}-${l.due.days ?? 0}`}>{templateLineText(l, tr)}</li>
          ))}
        </ul>
        {message && (
          <p role={message.tone === "danger" ? "alert" : "status"} style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
      </form>
    </Panel>
  );
}
