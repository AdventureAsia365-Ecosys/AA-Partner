"use client";
// web/components/ledger/SuppliersView.tsx — A-13 Suppliers (AA-944, card CRM-16; design v3 M15). Adventure Asia is
// pinned first and built in (never edited or deleted). Own suppliers: add, edit, delete (refused while live lines use
// them). The contact name and e-mail are never in the list: the drawer says whether each is filled in and opens one
// with a reason (POST /bff/suppliers/{id}/reveal, one audit event), the F1 reveal pattern. The default commission
// arrives only for owner and agent (the API's margin serializer strips it for the assistant).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { REVEAL_REASONS } from "@/lib/clients";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { errorKey, seesMargin, type Supplier } from "@/lib/ledger";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
  minWidth: 0,
};
const cell: React.CSSProperties = { padding: "8px 10px", borderTop: `1px solid ${K.line}`, verticalAlign: "top", fontSize: 13 };

function ContactReveal({ supplier, column, lang }: { supplier: Supplier; column: "contact_name" | "contact_email"; lang: Lang }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [reason, setReason] = useState("");
  const [value, setValue] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const filled = column === "contact_name" ? supplier.has_contact_name : supplier.has_contact_email;
  if (!filled) return <span style={{ color: K.muted }}>{tr("suppliers.none")}</span>;
  if (value !== null) return <span data-testid={`supplier-${column}-value`}>{value}</span>;
  async function show() {
    const res = await bffSend<{ value: string | null }>("POST", `suppliers/${supplier.agency_supplier_id}/reveal`, { field: column, reason });
    if (!res.ok) {
      setError(true);
      return;
    }
    setValue(res.data.value ?? "");
  }
  return (
    <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
      <span style={{ color: K.muted }}>{tr("suppliers.filled")}</span>
      <select aria-label={tr("suppliers.revealReason")} style={field} value={reason} onChange={(e) => setReason(e.target.value)}>
        <option value="">{tr("suppliers.revealReason")}</option>
        {REVEAL_REASONS.map((r) => (
          <option key={r} value={r}>
            {tr(`why.${r}`)}
          </option>
        ))}
      </select>
      <Button size="sm" disabled={!hydrated || !reason} onClick={show}>
        {tr("suppliers.reveal")}
      </Button>
      {error && <span style={{ color: K.danger, fontSize: 12 }}>{tr("ledger.error.other")}</span>}
    </span>
  );
}

export function SuppliersView({
  suppliers,
  categories,
  role,
  lang,
}: {
  suppliers: Supplier[];
  categories: string[];
  role: string;
  lang: Lang;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const margin = seesMargin(role);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown) {
    setBusy(true);
    setError(null);
    const res = await bffSend<unknown>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(errorKey(res.detail)));
      return false;
    }
    router.refresh();
    return true;
  }

  function bodyOf(form: FormData, creating: boolean) {
    const out: Record<string, unknown> = {
      name: String(form.get("name") ?? "").trim(),
      category: String(form.get("category") ?? "other"),
      confirmation_unique: form.get("confirmation_unique") === "on",
    };
    for (const key of ["contact_name", "contact_email"]) {
      const v = String(form.get(key) ?? "").trim();
      if (v || creating) out[key] = v || null;
    }
    if (margin) {
      const pct = String(form.get("default_commission_pct") ?? "").trim();
      out.default_commission_pct = pct === "" ? null : pct;
      // AA-957: when the supplier pays its commission (days after the service; empty = 30).
      const days = String(form.get("commission_due_days") ?? "").trim();
      out.commission_due_days = days === "" ? null : Number(days);
    }
    return out;
  }

  function form(supplier: Supplier | null) {
    const creating = supplier === null;
    return (
      <form
        action={async (data) => {
          const ok = creating
            ? await send("POST", "suppliers", bodyOf(data, true))
            : await send("PATCH", `suppliers/${supplier.agency_supplier_id}`, bodyOf(data, false));
          if (ok) {
            setAdding(false);
            setOpen(null);
          }
        }}
        style={{ display: "grid", gap: 8, maxWidth: 480, marginTop: 8 }}
        data-testid={creating ? "supplier-add-form" : "supplier-edit-form"}
      >
        <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
          {tr("suppliers.name")}
          <input name="name" style={field} required maxLength={120} defaultValue={supplier?.name ?? ""} />
        </label>
        <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
          {tr("suppliers.category")}
          <select name="category" style={field} defaultValue={supplier?.category ?? "accommodation"}>
            {categories.map((c) => (
              <option key={c} value={c}>
                {tr(`suppliers.category.${c}`)}
              </option>
            ))}
          </select>
        </label>
        <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
          {tr("suppliers.contactName")}
          <input name="contact_name" style={field} maxLength={200} placeholder={creating ? "" : tr("suppliers.keep")} />
        </label>
        <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
          {tr("suppliers.contactEmail")}
          <input name="contact_email" type="email" style={field} maxLength={200} placeholder={creating ? "" : tr("suppliers.keep")} />
        </label>
        {margin && (
          <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
            {tr("suppliers.commission")}
            <input name="default_commission_pct" style={field} inputMode="decimal" defaultValue={supplier?.default_commission_pct ?? ""} />
          </label>
        )}
        {margin && (
          <Field label={tr("supplier.commissionDueDays")} hint={tr("supplier.commissionDueDaysHint")}>
            <Input size="sm" name="commission_due_days" type="number" min={0} max={365} step={1} defaultValue={supplier?.commission_due_days ?? ""} data-testid="supplier-commission-due-days" />
          </Field>
        )}
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12.5 }}>
          <input type="checkbox" name="confirmation_unique" defaultChecked={supplier?.confirmation_unique ?? false} />
          {tr("suppliers.unique")}
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy}>
            {tr("ledger.save")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => (creating ? setAdding(false) : setOpen(null))}>
            {tr("ledger.close")}
          </Button>
        </div>
      </form>
    );
  }

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="suppliers-view">
      {error && (
        <p role="status" style={{ margin: 0, color: K.danger, fontSize: 13 }} data-testid="suppliers-error">
          {error}
        </p>
      )}
      <div style={{ overflowX: "auto", background: K.card, border: `1px solid ${K.line}`, borderRadius: 10 }}>
        <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 560 }}>
          <tbody>
            {suppliers.map((s) => (
              <tr key={s.agency_supplier_id} data-testid={`supplier-${s.kind}`}>
                <td style={cell}>
                  <strong>{s.name}</strong> {s.builtin && <Badge tone="accent">{tr("suppliers.builtin")}</Badge>}
                  <div style={{ color: K.muted, fontSize: 12 }}>{tr(`suppliers.category.${s.category}`)}</div>
                  {open === s.agency_supplier_id && !s.builtin && (
                    <div style={{ display: "grid", gap: 6, marginTop: 8, fontSize: 12.5 }}>
                      <div>
                        {tr("suppliers.contactName")}: <ContactReveal supplier={s} column="contact_name" lang={lang} />
                      </div>
                      <div>
                        {tr("suppliers.contactEmail")}: <ContactReveal supplier={s} column="contact_email" lang={lang} />
                      </div>
                      {form(s)}
                    </div>
                  )}
                </td>
                <td style={cell}>{s.line_count} {tr("suppliers.lines")}</td>
                {margin && <td style={cell}>{s.default_commission_pct ? `${s.default_commission_pct} %` : "—"}</td>}
                <td style={{ ...cell, whiteSpace: "nowrap" }}>
                  {!s.builtin && (
                    <>
                      <Button size="sm" disabled={!hydrated} onClick={() => setOpen(open === s.agency_supplier_id ? null : s.agency_supplier_id)}>
                        {tr("suppliers.open")}
                      </Button>{" "}
                      {role !== "assistant" && (
                        <Button size="sm" variant="danger" disabled={!hydrated || busy || s.line_count > 0} onClick={() => send("DELETE", `suppliers/${s.agency_supplier_id}`)}>
                          {tr("ledger.delete")}
                        </Button>
                      )}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {adding ? (
        form(null)
      ) : (
        <div>
          <Button variant="primary" size="sm" disabled={!hydrated} onClick={() => setAdding(true)}>
            {tr("suppliers.add")}
          </Button>
        </div>
      )}
    </div>
  );
}
