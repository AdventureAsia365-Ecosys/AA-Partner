"use client";
// web/components/partners/PartnerForm.tsx — AA-1071 (crm-056): add or edit a sales partner (owner only; the API and
// the database refuse anyone else): kind, name, contact e-mail and phone (sealed by the API; clearing them removes the
// partner's personal data beyond its name), the commission terms (percent / fixed per traveller / net rate, the rate,
// the currency of a per-traveller amount, counted on the trip price or the margin) and the payout days. A kit Modal;
// the fields wrap at phone width.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, Input, K, Modal, Select, useHydrated, useToast } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { BASES, type CommissionType, KINDS, partnerErrorKey, type PartnerDetail, TYPES } from "@/lib/partners";

type Draft = {
  kind: string;
  name: string;
  contact_email: string;
  contact_phone: string;
  commission_type: CommissionType;
  rate: string;
  currency: string;
  basis: string;
  payout_due_days: string;
};

function draftOf(p: PartnerDetail | null, base: string): Draft {
  return {
    kind: p?.kind ?? "sub_agent",
    name: p?.name ?? "",
    contact_email: p?.contact_email ?? "",
    contact_phone: p?.contact_phone ?? "",
    commission_type: p?.commission_type ?? "percent",
    rate: p ? String(Number(p.rate)) : "10",
    currency: p?.currency ?? base,
    basis: p?.basis ?? "revenue",
    payout_due_days: String(p?.payout_due_days ?? 30),
  };
}

/** The request body of the form (empty contact fields clear them). */
export function partnerBody(d: Draft): Record<string, unknown> {
  return {
    kind: d.kind,
    name: d.name.trim(),
    contact_email: d.contact_email.trim() || null,
    contact_phone: d.contact_phone.trim() || null,
    commission_type: d.commission_type,
    rate: d.rate.trim() || "0",
    currency: d.commission_type === "fixed_per_pax" ? d.currency.trim().toUpperCase() : null,
    basis: d.basis,
    payout_due_days: Number.parseInt(d.payout_due_days, 10) || 0,
  };
}

export function PartnerForm({ open, partner, base, lang, onClose }: { open: boolean; partner: PartnerDetail | null; base: string; lang: Lang; onClose: () => void }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [draft, setDraft] = useState<Draft>(() => draftOf(partner, base));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (key: keyof Draft) => (e: { target: { value: string } }) => setDraft({ ...draft, [key]: e.target.value } as Draft);
  const perPax = draft.commission_type === "fixed_per_pax";

  async function save() {
    setBusy(true);
    setError(null);
    const body = partnerBody(draft);
    const res = partner ? await bffSend("PATCH", `sales-partners/${partner.sales_partner_id}`, body) : await bffSend("POST", "sales-partners", body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(partnerErrorKey(res.detail)));
      return;
    }
    toast.success(tr("partners.saved"));
    onClose();
    router.refresh();
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr(partner ? "partners.form.titleEdit" : "partners.form.titleNew")}
      testId="partner-form"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {tr("partners.form.cancel")}
          </Button>
          <Button onClick={save} disabled={!hydrated || busy || !draft.name.trim()} testId="partner-save">
            {tr("partners.form.save")}
          </Button>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))" }}>
        <Field label={tr("partners.form.kind")}>
          <Select value={draft.kind} onChange={set("kind")} options={KINDS.map((k) => ({ value: k, label: tr(`partners.kind.${k}`) }))} />
        </Field>
        <Field label={tr("partners.form.name")}>
          <Input value={draft.name} onChange={set("name")} maxLength={120} data-testid="partner-name" />
        </Field>
        <Field label={tr("partners.form.email")} hint={tr("partners.form.emailHelp")}>
          <Input type="email" value={draft.contact_email} onChange={set("contact_email")} maxLength={200} data-testid="partner-email" />
        </Field>
        <Field label={tr("partners.form.phone")}>
          <Input type="tel" value={draft.contact_phone} onChange={set("contact_phone")} maxLength={40} />
        </Field>
        <Field label={tr("partners.form.type")}>
          <Select value={draft.commission_type} onChange={set("commission_type")} options={TYPES.map((v) => ({ value: v, label: tr(`partners.type.${v}`) }))} />
        </Field>
        <Field label={tr(perPax ? "partners.form.rateAmount" : "partners.form.ratePercent")}>
          <Input inputMode="decimal" value={draft.rate} onChange={set("rate")} data-testid="partner-rate" />
        </Field>
        {perPax ? (
          <Field label={tr("partners.form.currency")}>
            <Input value={draft.currency} onChange={set("currency")} maxLength={3} />
          </Field>
        ) : (
          <Field label={tr("partners.form.basis")}>
            <Select value={draft.basis} onChange={set("basis")} options={BASES.map((v) => ({ value: v, label: tr(`partners.basis.${v}`) }))} />
          </Field>
        )}
        <Field label={tr("partners.form.due")}>
          <Input inputMode="numeric" value={draft.payout_due_days} onChange={set("payout_due_days")} />
        </Field>
      </div>
      {error && (
        <p role="alert" style={{ margin: "12px 0 0", fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
    </Modal>
  );
}
