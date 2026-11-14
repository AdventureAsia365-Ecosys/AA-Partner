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
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export interface RetailMarkupView {
  retail_markup_pct: Record<string, string>;
  kinds: string[];
  /** AA-1096 L4: the highest markup AA allows (a platform setting); an API before it omits it (300). */
  markup_max_pct?: string;
}

/** The body of a save: only the kinds whose value changed (AA-1096 S280: a markup saved under an older, higher ceiling
 * is not sent again, so it never blocks a change of another kind). ``undefined`` names the first invalid kind. */
export function changedMarkups(
  kinds: string[],
  values: Record<string, string>,
  stored: Record<string, string>,
  max: number,
): { body: Record<string, number | null> } | { invalid: string } {
  const body: Record<string, number | null> = {};
  for (const kind of kinds) {
    const raw = (values[kind] ?? "").trim();
    const before = stored[kind] ?? "";
    if (raw === before || (raw !== "" && before !== "" && Number(raw) === Number(before))) continue;
    const parsed = markupInput(raw, max);
    if (parsed === undefined) return { invalid: kind };
    body[kind] = parsed;
  }
  return { body };
}

/** A stored markup above AA's current limit (kept until the owner changes it). */
export function aboveLimit(stored: string | undefined, max: number): boolean {
  return stored !== undefined && stored !== "" && Number(stored) > max;
}

/** "" → null (no markup); a percentage 0 to ``max`` with at most 2 decimals → its number; anything else → undefined. */
export function markupInput(value: string, max = 300): number | null | undefined {
  const v = value.trim();
  if (!v) return null;
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(v)) return undefined;
  const n = Number(v);
  return n <= max ? n : undefined;
}

export function RetailMarkupForm({ view, lang }: { view: RetailMarkupView; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const max = Number(view.markup_max_pct ?? "300");
  const tr = (key: string) => fmt(t(lang, key as MessageKey), { max: String(max) });
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(view.kinds.map((k) => [k, view.retail_markup_pct[k] ?? ""])),
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  const kindError = (kind: string, limit: string) =>
    fmt(t(lang, "markup.errorKind"), { kind: tr(`markup.kind.${kind}`), max: limit });

  async function save() {
    const changed = changedMarkups(view.kinds, values, view.retail_markup_pct, max);
    if ("invalid" in changed) {
      setMessage({ tone: "danger", text: kindError(changed.invalid, String(max)) });
      return;
    }
    if (Object.keys(changed.body).length === 0) {
      setMessage({ tone: "success", text: tr("markup.saved") });
      return;
    }
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "settings/retail-markup", { retail_markup_pct: changed.body });
    setBusy(false);
    if (!res.ok) {
      // the API names the product type and the limit it checked (the limit may have changed since the page loaded)
      const b = res.body as { kind?: unknown; max?: unknown } | undefined;
      const text =
        res.detail === "invalid_markup" && typeof b?.kind === "string"
          ? kindError(b.kind, typeof b.max === "string" ? b.max : String(max))
          : tr("markup.error");
      setMessage({ tone: "danger", text });
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
            <Field
              key={kind}
              label={`${tr(`markup.kind.${kind}`)} (%)`}
              hint={aboveLimit(view.retail_markup_pct[kind], max) ? tr("markup.aboveLimit") : undefined}
            >
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
