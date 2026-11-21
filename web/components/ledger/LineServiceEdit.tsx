"use client";
// web/components/ledger/LineServiceEdit.tsx — AA-1147: "Deadline and supplier" on an agency line of the Bookings tab.
// The lines an accepted proposal writes (crm.agency_lines_from_proposal) start without a deadline, so the agent gives
// one here (a ticketing time limit, a visa filing date): the hourly tick then opens the deadline task before it. While
// the line is planned the supplier changes too ("Handled by the agency" = none); a flight or the fee may be folded into
// the tour price. It sends PATCH lines/{id} with only what changed (lib/ledger.ts lineChangeBody); "Clear deadline"
// sends due_at null. Renders nothing on a line it does not apply to (canEditService).
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { canBundle } from "@/lib/agencyItems";
import type { DateLocale } from "@/lib/dates";
import { canEditService, lineChangeBody, type Line, type Supplier } from "@/lib/ledger";

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

type Props = {
  line: Line;
  suppliers: Supplier[];
  closed: boolean;
  disabled: boolean;
  locale: DateLocale;
  tr: (key: string) => string;
  /** The panel's PATCH lines/{id} (busy flag, refusal wording, refresh); true when the API took it. */
  onSave: (lineId: string, body: Record<string, unknown>) => Promise<boolean>;
};

function SupplierPick({ line, suppliers, tr }: Pick<Props, "line" | "suppliers" | "tr">) {
  return (
    <label style={{ display: "grid", gap: 2, fontSize: 12 }}>
      {tr("agencyItem.supplierOptional")}
      <select name="agency_supplier_id" style={field} defaultValue={line.agency_supplier_id ?? ""} data-testid="line-edit-supplier">
        <option value="">{tr("agencyItem.handledByAgency")}</option>
        {suppliers.map((s) => (
          <option key={s.agency_supplier_id} value={s.agency_supplier_id}>
            {s.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function BundledBox({ line, tr }: Pick<Props, "line" | "tr">) {
  return (
    <label style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: 12 }}>
      <input type="checkbox" name="bundled" defaultChecked={Boolean(line.bundled)} data-testid="line-edit-bundled" />
      <span>
        {tr("agencyItem.bundled")}
        <span style={{ display: "block", color: K.muted, fontSize: 11.5 }}>{tr("agencyItem.bundledHint")}</span>
      </span>
    </label>
  );
}

export function LineServiceEdit({ line, suppliers, closed, disabled, locale, tr, onSave }: Props) {
  const [open, setOpen] = useState(false);
  if (!canEditService(line, closed)) return null;
  if (!open) {
    return (
      <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setOpen(true)} testId="line-edit-service">
        {tr("agencyItem.editService")}
      </Button>
    );
  }

  async function save(form: FormData) {
    const body = lineChangeBody(form, line);
    if (Object.keys(body).length === 0 || (await onSave(line.booking_line_id, body))) setOpen(false);
  }

  async function clearDue() {
    if (await onSave(line.booking_line_id, { due_at: null })) setOpen(false);
  }

  return (
    <form action={save} style={{ display: "grid", gap: 8, marginTop: 8, maxWidth: 360, width: "100%", whiteSpace: "normal" }} data-testid="line-edit-form">
      <DateInput
        locale={locale}
        name="due_on"
        label={tr("agencyItem.dueAt")}
        help={tr("agencyItem.dueHint")}
        readAs={tr("settings.readAs")}
        invalid={tr("settings.invalidDate")}
        defaultValue={line.due_at ? line.due_at.slice(0, 10) : ""}
      />
      {line.status === "planning" && <SupplierPick line={line} suppliers={suppliers} tr={tr} />}
      {canBundle(line.category) && <BundledBox line={line} tr={tr} />}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button type="submit" size="sm" variant="primary" disabled={disabled}>
          {tr("ledger.save")}
        </Button>
        {line.due_at && (
          <Button size="sm" variant="ghost" disabled={disabled} onClick={clearDue} testId="line-edit-clear-due">
            {tr("agencyItem.clearDue")}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          {tr("ledger.close")}
        </Button>
      </div>
    </form>
  );
}
