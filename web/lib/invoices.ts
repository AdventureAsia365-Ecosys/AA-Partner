// web/lib/invoices.ts
// AA-952 (card CRM-23 slice 2) — the shapes of the invoice API (GET /crm/v1/trips/{id}/invoices, /invoices,
// /invoices/{id}, /settings/invoices) and the pure helpers the trip's Invoices card, A-10 Payments and Settings >
// Payments use: refusal wording, status tones, the typed-lines body, who may bill. Amounts are the client's sell
// amounts (no margin key ever comes back). Slice 3: payments against an invoice, refund requests (approve per the
// role matrix), the agency's own payment methods (GET /crm/v1/settings/payment-methods).

import { amountOf } from "./proposals";

export type InvoiceKind = "deposit" | "balance" | "service_fee" | "adjustment";
export type InvoiceStatus = "draft" | "open" | "payment_claimed" | "paid" | "paid_unfulfillable" | "void" | "expired";
export type InvoiceAction = "edit" | "issue" | "void" | "record_payment";

export interface InvoiceLine {
  line_id: string;
  position: number;
  kind: "charge" | "included";
  source: string;
  description: string;
  quantity: string | null;
  unit_amount: string | null;
  amount: string | null;
}

export interface Payment {
  payment_id: string;
  entry_kind: "payment" | "refund";
  amount: string;
  currency: string;
  method: "bank_transfer" | "cash" | "card_offline" | "other";
  received_on: string;
  provider: string;
  reference: string | null;
  refund_of: string | null;
  /** A payment: what can still be asked back. */
  refundable_amount?: string;
}

export type RefundStatus = "proposed" | "approved" | "rejected" | "executed";

export interface RefundRequest {
  refund_id: string;
  invoice_id: string;
  payment_id: string;
  amount: string;
  currency: string;
  reason: string;
  status: RefundStatus;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  executed_at: string | null;
  /** The A-10 list adds where it belongs. */
  trip_id?: string;
  trip_title?: string;
  invoice_number?: string | null;
}

export interface PaymentAccount {
  account_id: string;
  provider: "manual" | "stripe" | "vn_gateway";
  status: "active" | "disconnected";
  display_name: string;
  instructions: string | null;
}

export interface PaymentMethodsView {
  accounts: PaymentAccount[];
  providers: { provider: string; available: boolean }[];
}

export const REFUND_TONE: Record<RefundStatus, "neutral" | "info" | "success" | "warning" | "danger"> = {
  proposed: "warning",
  approved: "info",
  rejected: "neutral",
  executed: "success",
};

export interface Invoice {
  invoice_id: string;
  trip_id: string;
  kind: InvoiceKind;
  status: InvoiceStatus;
  number: string | null;
  schedule_id: string | null;
  proposal_id: string | null;
  currency: string;
  subtotal_amount: string;
  tax_amount: string;
  total_amount: string;
  tax_rate: string;
  tax_label: string;
  tax_inclusive: boolean;
  memo: string | null;
  due_on: string | null;
  issue_date: string | null;
  issued_at: string | null;
  paid_at: string | null;
  voided_at: string | null;
  void_reason: string | null;
  created_at: string;
  actions: InvoiceAction[];
  lines?: InvoiceLine[];
  /** Slice 3 (payments): what is still owed, what was paid / refunded, the payments and the refund requests. */
  balance_amount?: string;
  paid_amount?: string;
  refunded_amount?: string;
  payments?: Payment[];
  refund_requests?: RefundRequest[];
}

export interface Receivable {
  invoice_id: string;
  trip_id: string;
  trip_title: string;
  kind: InvoiceKind;
  status: InvoiceStatus;
  number: string | null;
  currency: string;
  total_amount: string;
  due_on: string | null;
  issue_date: string | null;
  paid_at: string | null;
  overdue: boolean;
  balance_amount?: string;
}

export interface ReceivablesView {
  today: string;
  status: InvoiceStatus | null;
  invoices: Receivable[];
}

export interface InvoiceSettings {
  invoice_prefix: string;
  invoice_tax_label: string;
  invoice_tax_rate: string;
  invoice_tax_inclusive: boolean;
  invoice_due_days: number;
  service_fee_enabled: boolean;
}

export const STATUSES: readonly InvoiceStatus[] = ["draft", "open", "payment_claimed", "paid", "paid_unfulfillable", "void", "expired"];
export const STATUS_TONE: Record<InvoiceStatus, "neutral" | "info" | "success" | "warning" | "danger"> = {
  draft: "neutral",
  open: "info",
  payment_claimed: "warning",
  paid: "success",
  paid_unfulfillable: "danger",
  void: "neutral",
  expired: "warning",
};

const ERRORS = new Set([
  "invoice_exists",
  "invoice_not_draft",
  "invoice_not_voidable",
  "invoice_has_payments",
  "invoice_empty",
  "schedule_not_due",
  "proposal_not_accepted",
  "schedule_invoice_fixed",
  "service_fee_disabled",
  "invalid_lines",
  "invalid_quantity",
  "invalid_unit_amount",
  "invalid_amount",
  "invalid_currency",
  "invalid_invoice_prefix",
  "invalid_tax_rate",
  "role_forbidden",
  "invoice_not_payable",
  "amount_above_balance",
  "amount_above_refundable",
  "received_in_future",
  "invalid_method",
  "payment_not_found",
  "payment_without_invoice",
  "refund_not_proposed",
  "refund_not_approved",
  "above_threshold",
  "provider_not_available",
]);

/** The i18n key of an invoice refusal (invoice.error.<code>, else a generic one). */
export function invoiceErrorKey(code: string): string {
  return ERRORS.has(code) ? `invoice.error.${code}` : "invoice.error.other";
}

/** Owner and agent bill (create, issue, void, record a payment); the assistant reads (the API decides). */
export function canBill(role: string): boolean {
  return role === "owner" || role === "agent";
}

/** The schedule rows still waiting for an invoice: due, and no live (non-void) invoice names them. */
export function uninvoicedRows<T extends { schedule_id: string; status: string }>(rows: T[], invoices: Invoice[]): T[] {
  const taken = new Set(invoices.filter((i) => i.status !== "void" && i.schedule_id).map((i) => i.schedule_id));
  return rows.filter((r) => r.status === "due" && !taken.has(r.schedule_id));
}

export interface LineDraft {
  description: string;
  quantity: string;
  unitAmount: string;
}

/** Typed charge lines -> the API body, or the i18n key of the first problem (blank rows are skipped). */
export function linesBody(
  rows: LineDraft[],
): { ok: true; lines: { description: string; quantity: string; unit_amount: string }[] } | { ok: false; key: string } {
  const lines = [];
  for (const row of rows) {
    const description = row.description.trim();
    if (!description && !row.unitAmount.trim()) continue;
    if (!description) return { ok: false, key: "invoice.error.description" };
    const quantity = row.quantity.trim() === "" ? "1" : amountOf(row.quantity);
    const unit = amountOf(row.unitAmount);
    if (!quantity) return { ok: false, key: "invoice.error.invalid_quantity" };
    if (!unit) return { ok: false, key: "invoice.error.invalid_unit_amount" };
    lines.push({ description, quantity, unit_amount: unit });
  }
  if (lines.length === 0) return { ok: false, key: "invoice.error.invalid_lines" };
  return { ok: true, lines };
}

/** Open the printable document (the API's escaped HTML with its own CSP) in a new tab to print or save as PDF. */
export function openDocument(html: string): void {
  if (typeof window === "undefined") return;
  const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  window.open(url, "_blank", "noopener");
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** Owner and agent may approve a refund (an agent only up to the agency threshold: the API says above_threshold). */
export function canDecideRefund(role: string): boolean {
  return role === "owner" || role === "agent";
}

/** A refund request's next step for this member: decide (proposed), execute (approved), or nothing. */
export function refundStep(r: RefundRequest, role: string): "decide" | "execute" | null {
  if (!canDecideRefund(role)) return null;
  if (r.status === "proposed") return "decide";
  if (r.status === "approved") return "execute";
  return null;
}
