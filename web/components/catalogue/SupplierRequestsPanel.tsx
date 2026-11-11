"use client";
// web/components/catalogue/SupplierRequestsPanel.tsx — AA-1069 (crm-053): "Request from suppliers" on the trip's
// Bookings tab. "Prepare e-mails" asks the API for one draft per supplier of the trip's planning agency lines (POST
// trips/{id}/supplier-requests: dates, travellers, rooms; traveller names when the member asks, by default for flight,
// insurance and visa suppliers). Each draft is shown editable and sent on its own from the agency's mailbox (POST
// .../supplier-requests/send: the normal send path with its 10 s Undo in Messages); its lines then read "Requested".
// A supplier without an e-mail gets no draft and a link to Suppliers. An inline group under the table (no dialog).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Checkbox, Field, Input, K, Textarea, useHydrated, useToast } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { catalogueErrorKey, type SupplierRequest, type SupplierRequests } from "@/lib/catalogue";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export function SupplierRequestsPanel({ tripId, lang, onClose }: { tripId: string; lang: Lang; onClose: () => void }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [names, setNames] = useState<boolean | null>(null);
  const [drafts, setDrafts] = useState<SupplierRequest[] | null>(null);
  const [sent, setSent] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function prepare() {
    setBusy(true);
    setError(null);
    const res = await bffSend<SupplierRequests>("POST", `trips/${tripId}/supplier-requests`, { include_names: names });
    setBusy(false);
    if (!res.ok) {
      setError(tr(catalogueErrorKey(res.detail)));
      return;
    }
    setDrafts(res.data.requests);
    setSent({});
  }

  function edit(id: string, field: "subject" | "body", value: string) {
    setDrafts((list) => (list ?? []).map((d) => (d.agency_supplier_id === id ? { ...d, [field]: value } : d)));
  }

  async function send(draft: SupplierRequest) {
    setBusy(true);
    setError(null);
    const res = await bffSend("POST", `trips/${tripId}/supplier-requests/send`, {
      message_id: draft.message_id,
      subject: draft.subject ?? "",
      body: draft.body ?? "",
      line_ids: draft.line_ids,
    });
    setBusy(false);
    if (!res.ok) {
      setError(tr(catalogueErrorKey(res.detail)));
      return;
    }
    setSent((s) => ({ ...s, [draft.agency_supplier_id]: true }));
    toast.success(fmt(tr("requests.sent"), { supplier: draft.supplier_name }));
    router.refresh();
  }

  return (
    <div role="group" aria-label={tr("requests.title")} data-testid="supplier-requests" style={{ display: "grid", gap: 10, padding: 12, borderRadius: 8, border: `1px solid ${K.line2}`, background: K.bg, minWidth: 0 }}>
      <strong style={{ fontSize: 13.5 }}>{tr("requests.title")}</strong>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("requests.hint")}</p>
      {drafts === null && (
        <>
          <Field as="div" label={tr("requests.names")} hint={tr("requests.namesHint")}>
            <Checkbox
              checked={names ?? false}
              indeterminate={names === null}
              onChange={(e) => setNames(e.target.checked)}
              label={tr("requests.names")}
              data-testid="supplier-requests-names"
            />
          </Field>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={prepare}>
              {tr("requests.build")}
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              {tr("requests.close")}
            </Button>
          </div>
        </>
      )}
      {drafts?.map((d) => (
        <div key={d.agency_supplier_id} data-testid="supplier-request" data-supplier={d.supplier_name} style={{ display: "grid", gap: 6, borderTop: `1px solid ${K.line}`, paddingTop: 8, minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
            <strong style={{ fontSize: 13, overflowWrap: "anywhere" }}>{d.supplier_name}</strong>
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <span style={{ fontSize: 12, color: K.muted }}>{fmt(tr("requests.lines"), { n: d.line_ids.length })}</span>
              {sent[d.agency_supplier_id] && <Badge tone="success">{tr("ledger.status.requested")}</Badge>}
            </span>
          </div>
          {!d.has_email ? (
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
              {fmt(tr("requests.noEmail"), { supplier: d.supplier_name })} <Link href="/suppliers">{tr("nav.suppliers")}</Link>
            </p>
          ) : (
            <>
              <Field label={tr("requests.subject")}>
                <Input value={d.subject ?? ""} maxLength={300} disabled={sent[d.agency_supplier_id]} onChange={(e) => edit(d.agency_supplier_id, "subject", e.target.value)} />
              </Field>
              <Field label={tr("requests.body")}>
                <Textarea rows={10} value={d.body ?? ""} disabled={sent[d.agency_supplier_id]} onChange={(e) => edit(d.agency_supplier_id, "body", e.target.value)} />
              </Field>
              {!sent[d.agency_supplier_id] && (
                <div>
                  <Button size="sm" variant="primary" disabled={!hydrated || busy || !(d.body ?? "").trim()} onClick={() => send(d)}>
                    {fmt(tr("requests.send"), { supplier: d.supplier_name })}
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      ))}
      {drafts !== null && (
        <div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            {tr("requests.close")}
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }} data-testid="supplier-requests-error">
          {error}
        </p>
      )}
    </div>
  );
}
