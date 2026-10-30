"use client";
// web/components/invoices/PaymentMethodsForm.tsx — Settings > Payments, "How clients pay you" (card CRM-23 slice 3;
// design v3 §6.3, BYO): the agency's own manual method (its name and the pay-by instructions printed on every invoice,
// e.g. the agency's own bank details or its office for cash) and the providers that are not available yet. Money always
// goes to the agency's own account; the CRM only records what arrived. Owner only (the page shows other roles why).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Textarea, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { invoiceErrorKey, type PaymentMethodsView } from "@/lib/invoices";

export function PaymentMethodsForm({ view, lang }: { view: PaymentMethodsView; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const manual = view.accounts.find((a) => a.provider === "manual" && a.status === "active");
  const [name, setName] = useState(manual?.display_name ?? tr("methods.defaultName"));
  const [instructions, setInstructions] = useState(manual?.instructions ?? "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function call(method: "PUT" | "POST", path: string, body: unknown, done: string) {
    setBusy(true);
    setMessage(null);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ tone: "danger", text: tr(invoiceErrorKey(res.detail)) });
      return;
    }
    setMessage({ tone: "success", text: tr(done) });
    router.refresh();
  }

  return (
    <Panel title={tr("methods.title")} testId="payment-methods">
      <form
        data-hydrated={hydrated ? "true" : undefined}
        style={{ display: "grid", gap: 10 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim() || !instructions.trim()) {
            setMessage({ tone: "danger", text: tr("methods.error.required") });
            return;
          }
          void call("PUT", "settings/payment-methods/manual", { display_name: name.trim(), instructions: instructions.trim() }, "methods.saved");
        }}
      >
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("methods.help")}</p>
        <Field label={tr("methods.name")}>
          <Input size="sm" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={tr("methods.instructions")} hint={tr("methods.instructionsHint")}>
          <Textarea rows={4} value={instructions} maxLength={2000} onChange={(e) => setInstructions(e.target.value)} />
        </Field>
        {message && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: message.tone === "danger" ? K.danger : K.success }}>
            {message.text}
          </p>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button type="submit" size="sm" disabled={!hydrated || busy}>
            {tr("methods.save")}
          </Button>
          {manual && (
            <Button type="button" size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => call("POST", `settings/payment-methods/${manual.account_id}/disconnect`, undefined, "methods.disconnected")}>
              {tr("methods.disconnect")}
            </Button>
          )}
        </div>
        <div style={{ display: "grid", gap: 4 }} data-testid="payment-providers">
          {view.providers
            .filter((p) => p.provider !== "manual")
            .map((p) => (
              <div key={p.provider} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                <span>{tr(`methods.provider.${p.provider}`)}</span>
                <Badge tone="neutral">{tr(p.available ? "methods.available" : "methods.notYet")}</Badge>
              </div>
            ))}
        </div>
      </form>
    </Panel>
  );
}
