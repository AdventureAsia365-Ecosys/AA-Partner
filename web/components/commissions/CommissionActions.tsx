"use client";
// web/components/commissions/CommissionActions.tsx — the follow-up of one commission (card CRM-27, Module 13; design
// v3 M13, R17): expected -> "Mark claimed" (the agency's own reference), "Record received" (amount, currency, the day
// it arrived: another currency is converted at that day's rate), "Write off" (a reason, a note), a new due date;
// claimed -> received, "Undo claim", write off. Owner and agent only (the API refuses the assistant). Used on A-12.
// S262 (AA-957 live): the success message is a page toast (kit ToastProvider in the CRM shell), so it stays in view
// when the row leaves the filter after router.refresh() (a received row leaves "To collect"); a refusal stays on the
// row. The forms wrap at phone width (every field min-width 0, the amount and currency side by side only when they fit).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, Input, K, Select, useHydrated, useToast } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { bffSend } from "@/lib/bffClient";
import { type Commission, commissionActions, commissionErrorKey, WRITE_OFF_REASONS } from "@/lib/commissions";
import type { DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";

type Form = null | "claim" | "receive" | "write_off" | "due";

export function CommissionActions({ commission, lang, locale }: { commission: Commission; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [form, setForm] = useState<Form>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState<string>(WRITE_OFF_REASONS[0]);
  const actions = commissionActions(commission);
  const base = `commissions/${commission.commission_id}`;

  async function send(method: "POST" | "PATCH", path: string, body: unknown, done: string) {
    setBusy(true);
    setError(null);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(commissionErrorKey(res.detail)));
      return;
    }
    toast.success(tr(done));
    setForm(null);
    router.refresh();
  }

  if (actions.length === 0) return null;
  const cancel = (
    <Button type="button" size="sm" variant="ghost" onClick={() => setForm(null)}>
      {tr("commission.close")}
    </Button>
  );
  return (
    <div data-testid="commission-actions" style={{ display: "grid", gap: 8, fontSize: 13, minWidth: 0, textAlign: "left" }}>
      {form === null && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {actions.includes("claim") && (
            <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setForm("claim")}>
              {tr("commission.claim")}
            </Button>
          )}
          {actions.includes("receive") && (
            <Button size="sm" disabled={!hydrated} onClick={() => setForm("receive")}>
              {tr("commission.receive")}
            </Button>
          )}
          {actions.includes("unclaim") && (
            <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => send("POST", `${base}/unclaim`, undefined, "commission.unclaimed")}>
              {tr("commission.unclaim")}
            </Button>
          )}
          {actions.includes("due") && (
            <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setForm("due")}>
              {tr("commission.saveDue")}
            </Button>
          )}
          {actions.includes("write_off") && (
            <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setForm("write_off")}>
              {tr("commission.writeOff")}
            </Button>
          )}
        </div>
      )}
      {form === "claim" && (
        <form
          data-testid="commission-claim-form"
          style={{ display: "grid", gap: 8, minWidth: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            const ref = String(new FormData(e.currentTarget).get("reference") ?? "").trim();
            void send("POST", `${base}/claim`, { reference: ref || null }, "commission.claimed");
          }}
        >
          <Field label={tr("commission.claimRef")} hint={tr("commission.claimRefHint")} style={{ minWidth: 0 }}>
            <Input size="sm" name="reference" maxLength={80} style={{ width: "100%" }} />
          </Field>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("commission.claim")}
            </Button>
            {cancel}
          </div>
        </form>
      )}
      {form === "receive" && (
        <form
          data-testid="commission-receive-form"
          style={{ display: "grid", gap: 8, minWidth: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            const data = new FormData(e.currentTarget);
            const day = String(data.get("received_on") ?? "");
            const ref = String(data.get("reference") ?? "").trim();
            void send(
              "POST",
              `${base}/receive`,
              {
                amount: String(data.get("amount") ?? "").trim(),
                currency: String(data.get("currency") ?? "").trim().toUpperCase(),
                received_on: day || null,
                reference: ref || null,
              },
              "commission.received",
            );
          }}
        >
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("commission.receiveHelp")}</p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Field label={tr("commission.amount")} style={{ flex: "1 1 120px", minWidth: 0 }}>
              <Input size="sm" name="amount" inputMode="decimal" required defaultValue={commission.commission_expected?.amount ?? ""} style={{ width: "100%" }} />
            </Field>
            <Field label={tr("commission.currency")} style={{ flex: "0 1 90px", minWidth: 0 }}>
              <Input size="sm" name="currency" maxLength={3} required defaultValue={commission.commission_expected?.currency ?? ""} style={{ width: "100%" }} />
            </Field>
          </div>
          <DateInput locale={locale} name="received_on" label={tr("commission.receivedOn")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
          <Field label={tr("commission.reference")} hint={tr("commission.referenceHint")} style={{ minWidth: 0 }}>
            <Input size="sm" name="reference" maxLength={80} style={{ width: "100%" }} />
          </Field>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("commission.receive")}
            </Button>
            {cancel}
          </div>
        </form>
      )}
      {form === "write_off" && (
        <form
          data-testid="commission-write-off-form"
          style={{ display: "grid", gap: 8, minWidth: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            const note = String(new FormData(e.currentTarget).get("note") ?? "").trim();
            void send("POST", `${base}/write-off`, { reason, note: note || null }, "commission.writtenOff");
          }}
        >
          <Field label={tr("commission.writeOffReason")}>
            <Select size="sm" value={reason} onChange={(e) => setReason(e.target.value)} options={WRITE_OFF_REASONS.map((r) => ({ value: r, label: tr(`commission.writeOffReason.${r}`) }))} />
          </Field>
          <Field label={tr("commission.note")} style={{ minWidth: 0 }}>
            <Input size="sm" name="note" maxLength={500} style={{ width: "100%" }} />
          </Field>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="submit" size="sm" variant="danger" disabled={!hydrated || busy}>
              {tr("commission.writeOff")}
            </Button>
            {cancel}
          </div>
        </form>
      )}
      {form === "due" && (
        <form
          data-testid="commission-due-form"
          style={{ display: "grid", gap: 8, minWidth: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            const day = String(new FormData(e.currentTarget).get("due_on") ?? "");
            if (day) void send("PATCH", base, { due_on: day }, "commission.dueSaved");
          }}
        >
          <DateInput locale={locale} name="due_on" label={tr("commission.dueOn")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} defaultValue={commission.due_on} />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("commission.save")}
            </Button>
            {cancel}
          </div>
        </form>
      )}
      {error && (
        <p role="alert" data-testid="commission-error" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>
          {error}
        </p>
      )}
    </div>
  );
}
