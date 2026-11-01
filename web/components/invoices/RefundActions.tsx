"use client";
// web/components/invoices/RefundActions.tsx — one refund request and its next step (card CRM-23 slice 3; design v3
// §4.6, C12): proposed -> Approve / Reject (owner any amount, an agent up to the agency threshold: the API answers
// above_threshold and the owner decides), approved -> "Mark refunded" with how and when the agency returned the money
// (it moved outside the CRM, from the agency's own account). Used on the trip's Invoices card and on A-10 Payments.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { invoiceErrorKey, REFUND_TONE, refundStep, type RefundRequest } from "@/lib/invoices";
import { formatAmount, PAY_METHODS } from "@/lib/proposals";

export function RefundActions({
  refund,
  role,
  lang,
  locale,
  onMessage,
  readOnly = false,
}: {
  refund: RefundRequest;
  role: string;
  lang: Lang;
  locale: DateLocale;
  onMessage: (tone: "danger" | "success", text: string) => void;
  /** S262 round 3: the trip's cancel run is being applied (TripClosed "updating"): the status only, no next step. */
  readOnly?: boolean;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const step = readOnly ? null : refundStep(refund, role);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [executing, setExecuting] = useState(false);
  const [method, setMethod] = useState("bank_transfer");

  async function send(path: string, body: unknown, done: string) {
    setBusy(true);
    const res = await bffSend("POST", `refund-requests/${refund.refund_id}/${path}`, body);
    setBusy(false);
    if (!res.ok) {
      onMessage("danger", tr(invoiceErrorKey(res.detail)));
      return;
    }
    onMessage("success", tr(done));
    setExecuting(false);
    router.refresh();
  }

  return (
    <div data-testid="refund-request" data-status={refund.status} style={{ display: "grid", gap: 6, fontSize: 13 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span>{fmt(tr("refund.amount"), { amount: formatAmount(refund.amount, refund.currency, locale) })}</span>
        <Badge tone={REFUND_TONE[refund.status]}>{tr(`refund.status.${refund.status}`)}</Badge>
        <span style={{ color: K.muted, overflowWrap: "anywhere" }}>{refund.reason}</span>
        {refund.executed_at && <span style={{ color: K.muted }}>{formatDate(refund.executed_at.slice(0, 10), locale)}</span>}
      </div>
      {refund.decision_note && <span style={{ color: K.muted, fontSize: 12.5 }}>{fmt(tr("refund.note"), { note: refund.decision_note })}</span>}
      {step === "decide" && (
        <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
          <Field label={tr("refund.noteLabel")} style={{ flex: "1 1 200px", minWidth: 0 }}>
            <Input size="sm" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
          </Field>
          <Button size="sm" disabled={!hydrated || busy} onClick={() => send("approve", { note: note.trim() || null }, "refund.approved")}>
            {tr("refund.approve")}
          </Button>
          <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => send("reject", { note: note.trim() || null }, "refund.rejected")}>
            {tr("refund.reject")}
          </Button>
        </div>
      )}
      {step === "execute" && !executing && (
        <div>
          <Button size="sm" disabled={!hydrated || busy} onClick={() => setExecuting(true)}>
            {tr("refund.markRefunded")}
          </Button>
        </div>
      )}
      {step === "execute" && executing && (
        <form
          data-testid="refund-execute-form"
          style={{ display: "grid", gap: 8 }}
          onSubmit={(e) => {
            e.preventDefault();
            const data = new FormData(e.currentTarget);
            const day = String(data.get("refunded_on") ?? "");
            const reference = String(data.get("reference") ?? "").trim();
            void send("execute", { method, refunded_on: day || null, reference: reference || null }, "refund.executed");
          }}
        >
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("refund.executeHelp")}</p>
          <Field label={tr("proposal.method")}>
            <Select size="sm" value={method} onChange={(e) => setMethod(e.target.value)} options={PAY_METHODS.map((m) => ({ value: m, label: tr(`proposal.method.${m}`) }))} />
          </Field>
          <DateInput locale={locale} name="refunded_on" label={tr("refund.refundedOn")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
          <Field label={tr("payment.reference")} hint={tr("payment.referenceHint")}>
            <Input size="sm" name="reference" maxLength={120} />
          </Field>
          <div style={{ display: "flex", gap: 8 }}>
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("refund.confirmRefunded")}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setExecuting(false)}>
              {tr("trip.cancel")}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
