"use client";
// web/components/proposals/ScheduleEditor.tsx — AA-1067 (crm-050, items crm-08 / crm-11): the builder's payment
// schedule and cancellation policy. A new version starts with the agency template (Settings > Payments; the AA default
// 30 % now, 40 % 60 days before departure, the rest 30 days before). Owner / agent edit the lines before publishing
// (each instalment a share and a date, the balance the rest); "Use the agency template" starts again; "Deposit and
// balance only" goes back to the two lines. The amounts are the API's (rounded per currency, the last line the rest).
// The cancellation policy goes out with the version (the agency default unless another current version is chosen).
import { useState } from "react";
import { Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import {
  addInstalment,
  draftLines,
  lineLabel,
  restPercent,
  scheduleBody,
  type DraftLine,
  type PolicyListing,
  type PolicySummary,
  type ScheduleAmount,
  type ScheduleLine,
} from "@/lib/retailPayments";

type Send = (method: "POST" | "PATCH", path: string, body?: Record<string, unknown>) => Promise<boolean>;

export function ScheduleEditor({
  proposalId,
  lines,
  amounts,
  policy,
  draft,
  canMoney,
  busy,
  locale,
  money,
  tr,
  write,
  onMessage,
}: {
  proposalId: string;
  lines: ScheduleLine[] | null;
  amounts: ScheduleAmount[] | undefined;
  policy: PolicySummary | null;
  draft: boolean;
  canMoney: boolean;
  busy: boolean;
  locale: DateLocale;
  money: (v: string | null | undefined) => string;
  tr: (key: string) => string;
  write: Send;
  onMessage: (tone: "danger" | "success", text: string) => void;
}) {
  const hydrated = useHydrated();
  const [rows, setRows] = useState<DraftLine[]>(() => draftLines(lines ?? []));
  const [policies, setPolicies] = useState<PolicyListing["policies"] | null>(null);
  const editable = draft && canMoney && lines !== null;

  /** The current versions the agent may choose (read when the agent asks to change it: no call on page load). */
  async function loadPolicies() {
    const res = await bffSend<PolicyListing>("GET", "cancellation-policies");
    setPolicies(res.ok ? (res.data.policies ?? []).filter((p) => p.status === "published") : []);
  }

  function set(i: number, patch: Partial<DraftLine>) {
    setRows(rows.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  }

  async function save(form: HTMLFormElement) {
    // The dates are the DateInput fields' ISO values (a typed date replaces the template's days).
    const data = new FormData(form);
    const dated = rows.map((r, i) => (r.kind === "deposit" ? r : { ...r, due_on: String(data.get(`due_${i}`) ?? "") || (r.days_before === null ? "" : r.due_on) }));
    const body = scheduleBody(dated.map((r, i) => (r.due_on !== rows[i].due_on ? { ...r, days_before: null } : r)));
    if (!body) {
      onMessage("danger", tr("schedule.invalid"));
      return;
    }
    if (await write("PATCH", `proposals/${proposalId}/builder`, { schedule: body })) onMessage("success", tr("schedule.saved"));
  }

  async function patch(body: Record<string, unknown>, done: string) {
    if (await write("PATCH", `proposals/${proposalId}/builder`, body)) onMessage("success", tr(done));
  }

  const dueText = (l: ScheduleLine) =>
    l.kind === "deposit" ? tr("schedule.atAccept") : l.due_on ? formatDate(l.due_on, locale) : l.days_before !== null ? fmt(tr("schedule.daysBefore"), { n: l.days_before }) : "—";

  return (
    <div data-testid="builder-schedule" style={{ display: "grid", gap: 8, marginTop: 14 }}>
      <strong style={{ fontSize: 13 }}>{tr("schedule.title")}</strong>
      {lines === null ? (
        <span style={{ fontSize: 12.5, color: K.muted }}>{tr("schedule.pair")}</span>
      ) : (
        <span style={{ fontSize: 12.5, color: K.muted }}>{tr("schedule.hint")}</span>
      )}
      {editable ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save(e.currentTarget);
          }}
          style={{ display: "grid", gap: 8 }}
        >
          {rows.map((r, i) => (
            <div key={`${r.kind}-${i}`} data-testid="builder-schedule-line" data-kind={r.kind} style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
              <span style={{ fontSize: 13, minWidth: 110 }}>{lineLabel({ kind: r.kind, seq: rows.slice(0, i + 1).filter((x) => x.kind === "instalment").length }, tr)}</span>
              {r.kind === "balance" ? (
                <span style={{ fontSize: 12.5, color: K.muted, minWidth: 100 }}>{fmt(tr("schedule.rest"), { pct: restPercent(rows) })}</span>
              ) : (
                <Field label={tr("schedule.percent")}>
                  <Input size="sm" inputMode="decimal" value={r.percent} onChange={(e) => set(i, { percent: e.target.value })} style={{ width: 90 }} />
                </Field>
              )}
              {r.kind === "deposit" ? (
                <span style={{ fontSize: 12.5, color: K.muted }}>{tr("schedule.atAccept")}</span>
              ) : (
                <DateInput
                  locale={locale}
                  name={`due_${i}`}
                  label={r.due_on || r.days_before === null ? tr("schedule.dueOn") : fmt(tr("schedule.daysBefore"), { n: r.days_before })}
                  readAs={tr("settings.readAs")}
                  invalid={tr("settings.invalidDate")}
                  defaultValue={r.due_on}
                />
              )}
              {amounts?.[i] && <span style={{ fontSize: 12.5 }}>{money(amounts[i].amount)}</span>}
              {r.kind === "instalment" && (
                <Button type="button" size="sm" variant="ghost" onClick={() => setRows(rows.filter((_, n) => n !== i))}>
                  {tr("schedule.remove")}
                </Button>
              )}
            </div>
          ))}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="button" size="sm" variant="secondary" disabled={rows.filter((r) => r.kind === "instalment").length >= 10} onClick={() => setRows(addInstalment(rows))}>
              {tr("schedule.add")}
            </Button>
            <span data-testid="builder-schedule-save">
              <Button type="submit" size="sm" disabled={!hydrated || busy}>
                {tr("schedule.save")}
              </Button>
            </span>
            <Button type="button" size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => void patch({ schedule_reset: true }, "schedule.saved")}>
              {tr("schedule.reset")}
            </Button>
          </div>
        </form>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, display: "grid", gap: 2 }}>
          {(amounts ?? []).map((l) => (
            <li key={`${l.kind}-${l.seq}`} data-kind={l.kind}>
              {lineLabel(l, tr)}: {money(l.amount)} · {dueText(l)}
            </li>
          ))}
        </ul>
      )}
      {draft && canMoney && lines === null && (
        <span>
          <Button type="button" size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => void patch({ schedule_reset: true }, "schedule.saved")}>
            {tr("schedule.reset")}
          </Button>
        </span>
      )}
      <div data-testid="builder-policy" style={{ display: "grid", gap: 4, fontSize: 13 }}>
        <span>{policy ? fmt(tr("policy.onProposal"), { name: policy.name, n: policy.version }) : tr("policy.onProposalNone")}</span>
        {draft && canMoney && policies === null && (
          <span>
            <Button type="button" size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => void loadPolicies()}>
              {tr("policy.change")}
            </Button>
          </span>
        )}
        {draft && canMoney && policies !== null && policies.length > 0 && (
          <Field label={tr("policy.choose")}>
            <Select
              size="sm"
              value={policy?.policy_id ?? ""}
              onChange={(e) => void patch({ cancellation_policy_id: e.target.value || null }, "schedule.saved")}
              options={[
                { value: "", label: tr("policy.onProposalNone") },
                ...policies.map((p) => ({ value: p.policy_id, label: `${p.name} · ${fmt(tr("policy.version"), { n: p.version })}` })),
              ]}
            />
          </Field>
        )}
      </div>
    </div>
  );
}
