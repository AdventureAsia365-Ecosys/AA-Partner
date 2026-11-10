"use client";
// web/components/finance/GroupCodeForm.tsx — AA-1068 (crm-32): the trip's own group departure code (PUT
// /trips/{id}/group-code). Trips with the same code share the group's costs by traveller; an empty code removes it.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, Input, K, useHydrated, useToast } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { financeErrorKey } from "@/lib/finance";
import { t, type Lang, type MessageKey } from "@/lib/i18n";

const CODE = /^[A-Za-z0-9:_.-]{0,80}$/;

export function GroupCodeForm({ tripId, code, lang, disabled }: { tripId: string; code: string | null; lang: Lang; disabled?: boolean }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [value, setValue] = useState(code ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = CODE.test(value.trim());

  async function save() {
    setBusy(true);
    setError(null);
    const res = await bffSend("PUT", `trips/${tripId}/group-code`, { group_code: value.trim() || null });
    setBusy(false);
    if (!res.ok) {
      setError(tr(financeErrorKey(res.detail)));
      return;
    }
    toast.success(tr("finance.group.saved"));
    router.refresh();
  }

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }} data-testid="group-code-form">
      <div style={{ flex: "1 1 200px", minWidth: 0 }}>
        <Field label={tr("finance.group.code")} hint={tr("finance.group.hint")} error={valid ? (error ?? undefined) : tr("finance.error.invalid_departure_ref")}>
          <Input value={value} onChange={(e) => setValue(e.target.value)} maxLength={80} disabled={disabled} placeholder="GRP-2027-03" />
        </Field>
      </div>
      <Button size="sm" variant="ghost" disabled={!hydrated || busy || disabled || !valid || value.trim() === (code ?? "")} onClick={save}>
        {tr("finance.group.save")}
      </Button>
      {busy && <span style={{ fontSize: 12, color: K.muted }}>…</span>}
    </div>
  );
}
