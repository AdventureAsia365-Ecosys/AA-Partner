"use client";
// web/components/partners/PartnerCommissionsTable.tsx — AA-1071 (crm-056): what the agency owes its sales partners,
// one row per booked trip and partner, on the kit DataTable (a card per row on a phone): the trip's short reference
// (a link to it), the partner, the basis and the amount (with its base-currency amount), the status with its due or
// paid date. The owner approves (the API recalculates first), sends back to approval, marks paid (date + own
// reference) or cancels; an agent reads only. Success in a page toast, a refusal on the row.
import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, DataTable, Input, K, useHydrated, useToast } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { type DateLocale, formatDate } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { commissionActions, type PartnerCommission, partnerErrorKey, STATUS_TONE } from "@/lib/partners";
import { formatAmount } from "@/lib/proposals";

const small: React.CSSProperties = { display: "block", fontSize: 12, color: K.muted, overflowWrap: "anywhere" };

function Actions({ c, lang }: { c: PartnerCommission; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [paying, setPaying] = useState(false);
  const [paidOn, setPaidOn] = useState(new Date().toISOString().slice(0, 10));
  const [ref, setRef] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = `partner-commissions/${c.partner_commission_id}`;

  async function send(action: string, body: unknown, done: MessageKey) {
    setBusy(true);
    setError(null);
    const res = await bffSend("POST", `${base}/${action}`, body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(partnerErrorKey(res.detail)));
      return;
    }
    toast.success(tr(done));
    setPaying(false);
    router.refresh();
  }

  const actions = commissionActions(c.status);
  if (actions.length === 0) return null;
  return (
    <span data-testid="partner-commission-actions" style={{ display: "grid", gap: 6, minWidth: 0 }}>
      {paying ? (
        <span style={{ display: "grid", gap: 6, minWidth: 0 }}>
          <label style={small}>
            {tr("partners.payDate")}
            <Input size="sm" type="date" value={paidOn} onChange={(e) => setPaidOn(e.target.value)} />
          </label>
          <label style={small}>
            {tr("partners.payRef")}
            <Input size="sm" value={ref} maxLength={120} onChange={(e) => setRef(e.target.value)} />
          </label>
          <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Button size="sm" disabled={busy} onClick={() => send("pay", { paid_on: paidOn, reference: ref.trim() || null }, "partners.paid")}>
              {tr("partners.pay")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setPaying(false)}>
              {tr("partners.form.cancel")}
            </Button>
          </span>
        </span>
      ) : (
        <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {actions.includes("approve") && (
            <Button size="sm" disabled={!hydrated || busy} onClick={() => send("approve", undefined, "partners.approved")} testId="partner-commission-approve">
              {tr("partners.approve")}
            </Button>
          )}
          {actions.includes("pay") && (
            <Button size="sm" disabled={!hydrated} onClick={() => setPaying(true)} testId="partner-commission-pay">
              {tr("partners.pay")}
            </Button>
          )}
          {actions.includes("unapprove") && (
            <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => send("unapprove", undefined, "partners.unapproved")}>
              {tr("partners.unapprove")}
            </Button>
          )}
          {actions.includes("cancel") && (
            <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => send("cancel", undefined, "partners.cancelled")}>
              {tr("partners.cancelCommission")}
            </Button>
          )}
        </span>
      )}
      {error && (
        <span role="alert" style={{ fontSize: 12, color: K.danger }}>
          {error}
        </span>
      )}
    </span>
  );
}

export function PartnerCommissionsTable({
  items,
  names,
  owner,
  lang,
  locale,
}: {
  items: PartnerCommission[];
  names: Record<string, string>;
  owner: boolean;
  lang: Lang;
  locale: DateLocale;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const money = (amount: string | null | undefined, currency: string) => formatAmount(amount, currency, locale);
  const columns: ColumnDef<PartnerCommission, unknown>[] = [
    {
      id: "trip",
      header: tr("partners.col.trip"),
      accessorFn: (c) => c.start_date ?? "",
      cell: ({ row }) => (
        <span data-testid="partner-commission-row" data-status={row.original.status} style={{ display: "block", minWidth: 0 }}>
          <Link href={`/pipeline/${row.original.trip_id}`}>{row.original.trip_ref}</Link>
          <span style={small}>{names[row.original.sales_partner_id] ?? ""}</span>
          {row.original.start_date && <span style={small}>{formatDate(row.original.start_date, locale)}</span>}
        </span>
      ),
    },
    {
      id: "amount",
      header: tr("partners.col.amount"),
      enableSorting: false,
      cell: ({ row }) => {
        const c = row.original;
        const basis =
          c.basis === "pax"
            ? fmt(tr("partners.paxShown"), { n: c.pax ?? 0, rate: money(c.rate, c.currency) })
            : fmt(tr("partners.basisShown"), { rate: `${Number(c.rate)}%`, basis: money(c.basis_amount, c.basis === "margin" ? (c.base_currency ?? c.currency) : c.currency) });
        return (
          <span style={{ display: "block", minWidth: 0 }}>
            <strong>{money(c.amount, c.currency)}</strong>
            <span style={small}>{basis}</span>
            {c.amount_base && c.base_currency && c.base_currency !== c.currency && <span style={small}>{money(c.amount_base, c.base_currency)}</span>}
          </span>
        );
      },
    },
    {
      id: "status",
      header: tr("partners.col.status"),
      accessorFn: (c) => c.due_on ?? "",
      cell: ({ row }) => {
        const c = row.original;
        return (
          <span style={{ display: "grid", gap: 4, justifyItems: "start", minWidth: 0 }}>
            <Badge tone={c.overdue ? "danger" : STATUS_TONE[c.status]}>{tr(c.overdue ? "partners.overdue" : `partners.pcStatus.${c.status}`)}</Badge>
            {c.status === "paid" && c.paid_on && <span style={small}>{fmt(tr("partners.paidOn"), { date: formatDate(c.paid_on, locale) })}</span>}
            {c.payout_ref && <span style={small}>{fmt(tr("partners.paidRef"), { ref: c.payout_ref })}</span>}
            {c.status !== "paid" && c.status !== "cancelled" && c.due_on && <span style={small}>{fmt(tr("partners.dueOn"), { date: formatDate(c.due_on, locale) })}</span>}
            {c.cancel_reason && <span style={small}>{tr(`partners.cancelReason.${c.cancel_reason}`)}</span>}
          </span>
        );
      },
    },
    ...(owner
      ? ([
          {
            id: "actions",
            header: tr("partners.col.actions"),
            enableSorting: false,
            cell: ({ row }) => <Actions c={row.original} lang={lang} />,
          },
        ] satisfies ColumnDef<PartnerCommission, unknown>[])
      : []),
  ];
  return <DataTable tableId="partner-commissions" data={items} columns={columns} getRowId={(c) => c.partner_commission_id} paginate={false} tableMinWidth={560} />;
}
