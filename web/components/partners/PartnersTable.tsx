"use client";
// web/components/partners/PartnersTable.tsx — AA-1071 (crm-056): the agency's sales partners on the kit DataTable (a
// card per partner on a phone). Every member sees the names (the trip's picker); owner and agent see the commission
// terms and what is still to pay; the owner edits, deactivates or deletes a partner (a partner a trip names is only
// deactivated: the API answers partner_in_use), issues or revokes its two links (the inquiry link /c/r/<token> and the
// partner page /c/a/<token>; the token is shown once, to copy) and downloads its statement CSV.
import type { ColumnDef } from "@tanstack/react-table";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, DataTable, Field, K, Modal, Select, useHydrated, useToast } from "@/app/_kit";
import { PartnerForm } from "@/components/partners/PartnerForm";
import { bffDownload, bffSend } from "@/lib/bffClient";
import type { DateLocale } from "@/lib/dates";
import { formatDate } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { canManage, type Partner, type PartnerDetail, partnerErrorKey, seesMoney, termsText } from "@/lib/partners";
import { clientUrl, formatAmount } from "@/lib/proposals";

const small: React.CSSProperties = { display: "block", fontSize: 12, color: K.muted, overflowWrap: "anywhere" };

export interface FormChoice {
  intake_form_id: string;
  label: string;
}

export function PartnersTable({
  partners,
  base,
  role,
  forms,
  clientOrigin,
  lang,
  locale,
}: {
  partners: Partner[];
  base: string;
  role: string;
  forms: FormChoice[];
  clientOrigin: string | null;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const money = (amount: string | null | undefined, currency: string) => formatAmount(amount, currency, locale);
  const owner = canManage(role);
  const [editing, setEditing] = useState<{ partner: PartnerDetail | null } | null>(null);
  const [linksOf, setLinksOf] = useState<Partner | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(method: "PATCH" | "DELETE", p: Partner, body: unknown, done: MessageKey) {
    setError(null);
    const res = await bffSend(method, `sales-partners/${p.sales_partner_id}`, body);
    if (!res.ok) {
      setError(tr(partnerErrorKey(res.detail)));
      return;
    }
    toast.success(tr(done));
    router.refresh();
  }

  async function edit(p: Partner) {
    const res = await bffSend<PartnerDetail>("GET", `sales-partners/${p.sales_partner_id}`);
    if (res.ok) setEditing({ partner: res.data });
    else setError(tr(partnerErrorKey(res.detail)));
  }

  async function statement(p: Partner) {
    const res = await bffDownload(`sales-partners/${p.sales_partner_id}/statement`, { filename: `partner-statement-${p.sales_partner_id.slice(0, 8)}.csv` });
    if (!res.ok && !res.redirecting) setError(tr("partners.statementError"));
  }

  const columns: ColumnDef<Partner, unknown>[] = [
    {
      id: "partner",
      header: tr("partners.col.partner"),
      accessorFn: (p) => p.name,
      cell: ({ row }) => (
        <span data-testid="partner-row" data-status={row.original.status} style={{ display: "grid", gap: 4, justifyItems: "start", minWidth: 0 }}>
          <strong style={{ overflowWrap: "anywhere" }}>{row.original.name}</strong>
          <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Badge tone="neutral">{tr(`partners.kind.${row.original.kind}`)}</Badge>
            {row.original.status === "inactive" && <Badge tone="warning">{tr("partners.status.inactive")}</Badge>}
          </span>
        </span>
      ),
    },
    ...(seesMoney(role)
      ? ([
          {
            id: "terms",
            header: tr("partners.col.terms"),
            enableSorting: false,
            cell: ({ row }) => (
              <span style={{ display: "block", minWidth: 0 }} data-testid="partner-terms">
                <span style={{ display: "block" }}>{termsText(lang, row.original, money)}</span>
                {row.original.payout_due_days !== undefined && <span style={small}>{fmt(tr("partners.terms.due"), { days: row.original.payout_due_days })}</span>}
              </span>
            ),
          },
          {
            id: "open",
            header: tr("partners.col.open"),
            enableSorting: false,
            cell: ({ row }) => (
              <span style={{ display: "block", minWidth: 0 }}>
                <span style={small}>{tr("partners.commissions.total.accrued")}</span>
                {money(row.original.accrued_base ?? "0", base)}
                <span style={small}>{tr("partners.commissions.total.approved")}</span>
                {money(row.original.approved_base ?? "0", base)}
              </span>
            ),
          },
        ] satisfies ColumnDef<Partner, unknown>[])
      : []),
    {
      id: "trips",
      header: tr("partners.col.trips"),
      accessorFn: (p) => p.trip_count,
      cell: ({ row }) => <span>{fmt(tr("partners.trips"), { n: row.original.trip_count })}</span>,
    },
    ...(owner || seesMoney(role)
      ? ([
          {
            id: "actions",
            header: tr("partners.col.actions"),
            enableSorting: false,
            cell: ({ row }) => {
              const p = row.original;
              return (
                <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {owner && (
                    <>
                      <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => edit(p)} testId="partner-edit">
                        {tr("partners.edit")}
                      </Button>
                      <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => setLinksOf(p)} testId="partner-links">
                        {tr("partners.links")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!hydrated}
                        onClick={() => act("PATCH", p, { status: p.status === "active" ? "inactive" : "active" }, "partners.saved")}
                      >
                        {tr(p.status === "active" ? "partners.deactivate" : "partners.activate")}
                      </Button>
                      {p.trip_count === 0 && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!hydrated}
                          onClick={() => window.confirm(tr("partners.deleteConfirm")) && act("DELETE", p, undefined, "partners.deleted")}
                        >
                          {tr("partners.delete")}
                        </Button>
                      )}
                    </>
                  )}
                  <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => statement(p)} testId="partner-statement">
                    {tr("partners.statement")}
                  </Button>
                </span>
              );
            },
          },
        ] satisfies ColumnDef<Partner, unknown>[])
      : []),
  ];

  return (
    <div style={{ display: "grid", gap: 10, minWidth: 0 }}>
      {owner && (
        <div>
          <Button onClick={() => setEditing({ partner: null })} disabled={!hydrated} testId="partner-add">
            {tr("partners.add")}
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      {partners.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="partners-empty">
          {tr("partners.empty")}
        </p>
      ) : (
        <DataTable tableId="sales-partners" data={partners} columns={columns} getRowId={(p) => p.sales_partner_id} paginate={false} tableMinWidth={560} />
      )}
      {editing && <PartnerForm open partner={editing.partner} base={base} lang={lang} onClose={() => setEditing(null)} />}
      {linksOf && <PartnerLinks partner={linksOf} forms={forms} clientOrigin={clientOrigin} lang={lang} locale={locale} onClose={() => setLinksOf(null)} />}
    </div>
  );
}

function PartnerLinks({
  partner,
  forms,
  clientOrigin,
  lang,
  locale,
  onClose,
}: {
  partner: Partner;
  forms: FormChoice[];
  clientOrigin: string | null;
  lang: Lang;
  locale: DateLocale;
  onClose: () => void;
}) {
  const router = useRouter();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [formId, setFormId] = useState(partner.links?.intake?.intake_form_id ?? forms[0]?.intake_form_id ?? "");
  const [made, setMade] = useState<Partial<Record<"intake" | "view", string>>>({});
  const [error, setError] = useState<string | null>(null);

  async function issue(purpose: "intake" | "view") {
    setError(null);
    const res = await bffSend<{ path: string }>("POST", `sales-partners/${partner.sales_partner_id}/links`, purpose === "intake" ? { purpose, intake_form_id: formId || null } : { purpose });
    if (!res.ok) {
      setError(tr(partnerErrorKey(res.detail)));
      return;
    }
    setMade({ ...made, [purpose]: clientUrl(clientOrigin, res.data.path) });
    router.refresh();
  }

  async function revoke(linkId: string) {
    const res = await bffSend("POST", `sales-partners/${partner.sales_partner_id}/links/${linkId}/revoke`);
    if (!res.ok) {
      setError(tr(partnerErrorKey(res.detail)));
      return;
    }
    toast.success(tr("partners.link.revoked"));
    onClose();
    router.refresh();
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      toast.success(tr("partners.link.copied"));
    } catch {
      /* the field below stays selectable */
    }
  }

  const block = (purpose: "intake" | "view") => {
    const live = partner.links?.[purpose];
    const url = made[purpose];
    return (
      <section key={purpose} data-testid={`partner-link-${purpose}`} style={{ display: "grid", gap: 8, minWidth: 0 }}>
        <strong>{tr(`partners.link.${purpose}`)}</strong>
        <span style={small}>{tr(`partners.link.${purpose}Help`)}</span>
        {live ? (
          <span style={small}>
            {fmt(tr("partners.link.until"), { date: formatDate(live.expires_at, locale) })} · {fmt(tr("partners.link.used"), { n: live.use_count })}
          </span>
        ) : (
          <span style={small}>{tr("partners.link.none")}</span>
        )}
        {purpose === "intake" && (
          <Field label={tr("partners.link.form")}>
            <Select value={formId} onChange={(e) => setFormId(e.target.value)} options={forms.map((f) => ({ value: f.intake_form_id, label: f.label }))} />
          </Field>
        )}
        {url && (
          <span style={{ display: "grid", gap: 6, minWidth: 0 }}>
            <code data-testid={`partner-link-url-${purpose}`} style={{ fontSize: 12, overflowWrap: "anywhere", userSelect: "all" }}>
              {url}
            </code>
            <span style={small}>{tr("partners.link.once")}</span>
            <span>
              <Button size="sm" variant="ghost" onClick={() => copy(url)}>
                {tr("partners.link.copy")}
              </Button>
            </span>
          </span>
        )}
        <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <Button size="sm" onClick={() => issue(purpose)} testId={`partner-link-new-${purpose}`}>
            {tr("partners.link.new")}
          </Button>
          {live && !url && (
            <Button size="sm" variant="ghost" onClick={() => revoke(live.link_id)}>
              {tr("partners.link.revoke")}
            </Button>
          )}
        </span>
      </section>
    );
  };

  return (
    <Modal open onClose={onClose} title={`${tr("partners.links")}: ${partner.name}`} testId="partner-links-modal">
      <div style={{ display: "grid", gap: 18 }}>
        {block("intake")}
        {block("view")}
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
