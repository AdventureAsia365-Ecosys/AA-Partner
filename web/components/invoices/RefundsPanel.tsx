"use client";
// web/components/invoices/RefundsPanel.tsx — A-10 Payments, the refund requests to act on (card CRM-23 slice 3;
// design v3 §4.6): proposed ones to approve or reject, approved ones to mark refunded, each linked to its trip and
// invoice. Every role sees the list; owner and agent act (an agent above the agency threshold is told to ask the owner).
import Link from "next/link";
import { useState } from "react";
import { K } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { RefundActions } from "@/components/invoices/RefundActions";
import type { DateLocale } from "@/lib/dates";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import type { RefundRequest } from "@/lib/invoices";

export function RefundsPanel({
  refunds,
  role,
  lang,
  locale,
}: {
  refunds: RefundRequest[];
  role: string;
  lang: Lang;
  locale: DateLocale;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  return (
    <Panel title={tr("refund.panel")} testId="refund-requests">
      <div style={{ display: "grid", gap: 10 }}>
        {message && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        {refunds.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("refund.none")}</p>}
        {refunds.map((r) => (
          <div key={r.refund_id} style={{ display: "grid", gap: 4, paddingTop: 8, borderTop: `1px solid ${K.line}` }}>
            <span style={{ fontSize: 12.5 }}>
              <strong>{r.invoice_number ?? tr("invoice.draftNumber")}</strong>
              {r.trip_id && (
                <>
                  {" · "}
                  <Link href={`/pipeline/${r.trip_id}`}>{r.trip_title}</Link>
                </>
              )}
            </span>
            <RefundActions refund={r} role={role} lang={lang} locale={locale} onMessage={(tone, text) => setMessage({ tone, text })} />
          </div>
        ))}
      </div>
    </Panel>
  );
}
