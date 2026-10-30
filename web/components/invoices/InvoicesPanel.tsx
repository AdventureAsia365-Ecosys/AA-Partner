"use client";
// web/components/invoices/InvoicesPanel.tsx — the trip's Invoices card (card CRM-23 slice 2; design v3 M7, R27,
// A-03 Payments): the trip's invoices with their status, total and due date; "Create invoice" for a due schedule row
// of the accepted proposal (it bills that row, tax included); a typed invoice (adjustment, or a service fee when the
// agency turned them on) with its own lines; Issue (the agency's next number), Void (with a reason) and the
// printable document (a new tab: print or save as PDF). Owner and agent bill; the assistant reads. Every refusal is
// worded from its code (lib/invoices.ts). Buttons wait for hydration (the page is a full server render).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Checkbox, Field, Input, K, Select, Textarea, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  canBill,
  invoiceErrorKey,
  linesBody,
  openDocument,
  STATUS_TONE,
  uninvoicedRows,
  type Invoice,
  type LineDraft,
} from "@/lib/invoices";
import { formatAmount } from "@/lib/proposals";

interface ScheduleRow {
  schedule_id: string;
  kind: "deposit" | "balance";
  amount: string;
  currency: string;
  due_on: string;
  status: string;
}

const EMPTY_LINE: LineDraft = { description: "", quantity: "1", unitAmount: "" };

export function InvoicesPanel({
  tripId,
  invoices,
  schedule,
  role,
  lang,
  locale,
}: {
  tripId: string;
  invoices: Invoice[];
  schedule: ScheduleRow[];
  role: string;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const bills = canBill(role);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [voiding, setVoiding] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [typing, setTyping] = useState(false);
  const [kind, setKind] = useState<"adjustment" | "service_fee">("adjustment");
  const [currency, setCurrency] = useState(schedule[0]?.currency ?? "USD");
  const [rows, setRows] = useState<LineDraft[]>([{ ...EMPTY_LINE }]);
  const [includeTrip, setIncludeTrip] = useState(false);
  const [memo, setMemo] = useState("");
  const open = uninvoicedRows(schedule, invoices);

  async function run<T>(method: "GET" | "POST" | "PATCH", path: string, body?: unknown, done?: string): Promise<T | null> {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(invoiceErrorKey(res.detail)) });
      return null;
    }
    if (done) setMessage({ tone: "success", text: tr(done) });
    if (method !== "GET") router.refresh();
    return res.data;
  }

  async function createTyped(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const built = linesBody(rows);
    if (!built.ok) {
      setMessage({ tone: "danger", text: tr(built.key) });
      return;
    }
    if (!/^[A-Z]{3}$/.test(currency)) {
      setMessage({ tone: "danger", text: tr("invoice.error.invalid_currency") });
      return;
    }
    const body = { kind, currency, lines: built.lines, memo: memo.trim() || null, include_trip_lines: includeTrip };
    if (await run("POST", `trips/${tripId}/invoices`, body, "invoice.created")) {
      setTyping(false);
      setRows([{ ...EMPTY_LINE }]);
      setMemo("");
    }
  }

  async function showDocument(invoiceId: string) {
    const doc = await run<{ html: string }>("GET", `invoices/${invoiceId}/document`);
    if (doc) openDocument(doc.html);
  }

  async function voidInvoice(invoiceId: string) {
    if (!reason.trim()) {
      setMessage({ tone: "danger", text: tr("invoice.error.reason") });
      return;
    }
    if (await run("POST", `invoices/${invoiceId}/void`, { reason: reason.trim() }, "invoice.voided")) {
      setVoiding(null);
      setReason("");
    }
  }

  const setRow = (i: number, patch: Partial<LineDraft>) =>
    setRows((current) => current.map((row, j) => (j === i ? { ...row, ...patch } : row)));

  return (
    <Panel title={tr("invoice.panel")} testId="trip-invoices">
      <div data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 12, minWidth: 0 }}>
        {message && (
          <p role="status" data-testid="invoice-message" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        {invoices.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("invoice.none")}</p>}
        {invoices.map((inv) => (
          <div key={inv.invoice_id} data-testid="invoice-row" data-status={inv.status} style={{ display: "grid", gap: 6, padding: "8px 0", borderTop: `1px solid ${K.line}` }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <strong>{inv.number ?? tr("invoice.draftNumber")}</strong>
              <span>{tr(`invoice.kind.${inv.kind}`)}</span>
              <span>{formatAmount(inv.total_amount, inv.currency, locale)}</span>
              {inv.due_on && <span style={{ color: K.muted }}>{fmt(tr("invoice.dueOn"), { date: formatDate(inv.due_on, locale) })}</span>}
              <Badge tone={STATUS_TONE[inv.status]}>{tr(`invoice.status.${inv.status}`)}</Badge>
            </div>
            {inv.void_reason && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{fmt(tr("invoice.voidReason"), { reason: inv.void_reason })}</p>}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => showDocument(inv.invoice_id)}>
                {tr("invoice.document")}
              </Button>
              {bills && inv.actions.includes("issue") && (
                <Button size="sm" disabled={!hydrated || busy} onClick={() => run("POST", `invoices/${inv.invoice_id}/issue`, undefined, "invoice.issued")}>
                  {tr("invoice.issue")}
                </Button>
              )}
              {bills && inv.actions.includes("void") && voiding !== inv.invoice_id && (
                <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => setVoiding(inv.invoice_id)}>
                  {tr("invoice.void")}
                </Button>
              )}
            </div>
            {voiding === inv.invoice_id && (
              <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }} data-testid="invoice-void-form">
                <Field label={tr("invoice.voidReasonLabel")} style={{ minWidth: 220, flex: "1 1 220px" }}>
                  <Input size="sm" value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
                </Field>
                <Button size="sm" variant="danger" disabled={!hydrated || busy} onClick={() => voidInvoice(inv.invoice_id)}>
                  {tr("invoice.confirmVoid")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setVoiding(null)}>
                  {tr("trip.cancel")}
                </Button>
              </div>
            )}
          </div>
        ))}

        {bills && open.length > 0 && (
          <section data-testid="invoice-from-schedule" style={{ display: "grid", gap: 6 }}>
            <h3 style={{ margin: 0, fontSize: 13, color: K.ink }}>{tr("invoice.toInvoice")}</h3>
            {open.map((row) => (
              <div key={row.schedule_id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                <span>{tr(`invoice.kind.${row.kind}`)}</span>
                <span>{formatAmount(row.amount, row.currency, locale)}</span>
                <Button size="sm" disabled={!hydrated || busy} onClick={() => run("POST", `trips/${tripId}/invoices`, { schedule_id: row.schedule_id }, "invoice.created")}>
                  {tr("invoice.create")}
                </Button>
              </div>
            ))}
          </section>
        )}

        {bills && !typing && (
          <div>
            <Button size="sm" variant="secondary" disabled={!hydrated || busy} onClick={() => setTyping(true)}>
              {tr("invoice.newTyped")}
            </Button>
          </div>
        )}
        {bills && typing && (
          <form onSubmit={createTyped} data-testid="invoice-typed-form" style={{ display: "grid", gap: 8, minWidth: 0 }}>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Field label={tr("invoice.kindLabel")}>
                <Select
                  size="sm"
                  value={kind}
                  onChange={(e) => setKind(e.target.value as "adjustment" | "service_fee")}
                  options={[
                    { value: "adjustment", label: tr("invoice.kind.adjustment") },
                    { value: "service_fee", label: tr("invoice.kind.service_fee") },
                  ]}
                />
              </Field>
              <Field label={tr("proposal.currency")}>
                <Input size="sm" value={currency} maxLength={3} style={{ width: 80 }} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
              </Field>
            </div>
            {rows.map((row, i) => (
              <div key={i} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }} data-testid="invoice-line-input">
                <Field label={tr("invoice.description")} style={{ flex: "3 1 220px", minWidth: 0 }}>
                  <Input size="sm" value={row.description} maxLength={300} onChange={(e) => setRow(i, { description: e.target.value })} />
                </Field>
                <Field label={tr("invoice.quantity")} style={{ flex: "0 1 90px" }}>
                  <Input size="sm" inputMode="decimal" value={row.quantity} onChange={(e) => setRow(i, { quantity: e.target.value })} />
                </Field>
                <Field label={tr("invoice.unitAmount")} style={{ flex: "1 1 120px" }}>
                  <Input size="sm" inputMode="decimal" value={row.unitAmount} onChange={(e) => setRow(i, { unitAmount: e.target.value })} />
                </Field>
              </div>
            ))}
            <div>
              <Button type="button" size="sm" variant="ghost" disabled={rows.length >= 50} onClick={() => setRows((r) => [...r, { ...EMPTY_LINE }])}>
                {tr("invoice.addLine")}
              </Button>
            </div>
            <Field label={tr("invoice.memo")} hint={tr("invoice.memoHint")}>
              <Textarea rows={2} value={memo} maxLength={2000} onChange={(e) => setMemo(e.target.value)} />
            </Field>
            <Checkbox label={tr("invoice.includeTrip")} checked={includeTrip} onChange={(e) => setIncludeTrip(e.target.checked)} />
            <div style={{ display: "flex", gap: 8 }}>
              <Button type="submit" size="sm" disabled={!hydrated || busy}>
                {tr("invoice.saveDraft")}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setTyping(false)}>
                {tr("trip.cancel")}
              </Button>
            </div>
          </form>
        )}
      </div>
    </Panel>
  );
}
