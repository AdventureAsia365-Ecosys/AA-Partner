"use client";
// web/components/proposals/RetailMarkupForm.tsx — Settings > Payments, "Retail markup on AA products" (AA-1030; root
// ADR 0019 b15): the owner's default markup in percent on AA's B2B price, per AA product type. A new AA option added
// without a sell gets AA's price plus this markup; with none, a tour gets AA's suggested retail price. Owner only (the
// page shows other roles why). Empty field = no markup for that type.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, Input, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";

export interface RetailMarkupView {
  retail_markup_pct: Record<string, string>;
  kinds: string[];
}

/** "" → null (no markup); a percentage 0-300 with at most 2 decimals → its number; anything else → undefined. */
export function markupInput(value: string): number | null | undefined {
  const v = value.trim();
  if (!v) return null;
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(v)) return undefined;
  const n = Number(v);
  return n <= 300 ? n : undefined;
}

export function RetailMarkupForm({ view, lang }: { view: RetailMarkupView; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(view.kinds.map((k) => [k, view.retail_markup_pct[k] ?? ""])),
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function save() {
    const body: Record<string, number | null> = {};
    for (const kind of view.kinds) {
      const parsed = markupInput(values[kind] ?? "");
      if (parsed === undefined) {
        setMessage({ tone: "danger", text: tr("markup.error") });
        return;
      }
      body[kind] = parsed;
    }
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "settings/retail-markup", { retail_markup_pct: body });
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr("markup.error") });
      return;
    }
    setMessage({ tone: "success", text: tr("markup.saved") });
    router.refresh();
  }

  return (
    <Panel title={tr("markup.title")} testId="retail-markup">
      <form
        data-hydrated={hydrated ? "true" : undefined}
        style={{ display: "grid", gap: 10 }}
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("markup.help")}</p>
        <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))" }}>
          {view.kinds.map((kind) => (
            <Field key={kind} label={`${tr(`markup.kind.${kind}`)} (%)`}>
              <Input
                size="sm"
                inputMode="decimal"
                placeholder={tr("markup.none")}
                value={values[kind] ?? ""}
                onChange={(e) => setValues({ ...values, [kind]: e.target.value })}
              />
            </Field>
          ))}
        </div>
        {message && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        <div>
          <Button type="submit" size="sm" disabled={!hydrated || busy}>
            {tr("markup.save")}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
