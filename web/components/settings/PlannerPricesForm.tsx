"use client";
// web/components/settings/PlannerPricesForm.tsx — Settings > Payments, "Prices on my planner and rounding" (AA-1152;
// S285 decision B2 option C, crm-086). The owner picks the rounding step per currency of every price the CRM suggests
// (AA items in proposals and the "from" prices on its trip planner: always rounded up, per traveller) from the steps AA
// offers, and switches the prices on its planner on or off. The API queues a planner publish after each save. Owner
// only (the page shows other roles why).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Field, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { changedSteps, type PlannerPricesView } from "@/lib/aaAddons";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const select: React.CSSProperties = { font: "inherit", fontSize: 13, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line2}`, background: K.card, color: K.ink, width: "100%" };

export function PlannerPricesForm({ view, lang }: { view: PlannerPricesView; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string, vars: Record<string, string> = {}) => fmt(t(lang, key as MessageKey), vars);
  const [show, setShow] = useState(view.show_planner_prices);
  const [picked, setPicked] = useState<Record<string, string>>(() =>
    Object.fromEntries(view.currencies.map((c) => [c.currency, c.chosen ? c.step : ""])),
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "danger" | "success"; text: string } | null>(null);

  async function save() {
    setBusy(true);
    setMessage(null);
    const res = await bffSend("PUT", "settings/planner-prices", { steps: changedSteps(view.currencies, picked), show_planner_prices: show });
    setBusy(false);
    if (!res.ok) {
      const b = res.body as { currency?: unknown } | undefined;
      const text =
        res.detail === "invalid_round_step" && typeof b?.currency === "string"
          ? tr("plannerPrices.errorStep", { currency: b.currency })
          : tr("plannerPrices.error");
      if (!res.redirecting) setMessage({ tone: "danger", text });
      return;
    }
    setMessage({ tone: "success", text: tr("plannerPrices.saved") });
    router.refresh();
  }

  return (
    <Panel title={tr("plannerPrices.title")} testId="planner-prices">
      <form
        data-hydrated={hydrated ? "true" : undefined}
        style={{ display: "grid", gap: 10 }}
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("plannerPrices.help")}</p>
        <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13 }}>
          <input type="checkbox" checked={show} onChange={(e) => setShow(e.target.checked)} data-testid="planner-prices-show" />
          <span>
            {tr("plannerPrices.show")}
            <span style={{ display: "block", color: K.muted, fontSize: 12 }}>{tr("plannerPrices.showHelp")}</span>
          </span>
        </label>
        <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))" }}>
          {view.currencies.map((c) => (
            <Field key={c.currency} label={tr("plannerPrices.step", { currency: c.currency })}>
              <select
                style={select}
                value={picked[c.currency] ?? ""}
                onChange={(e) => setPicked({ ...picked, [c.currency]: e.target.value })}
                data-testid={`planner-prices-step-${c.currency}`}
              >
                <option value="">{tr("plannerPrices.default", { step: c.default })}</option>
                {c.choices.map((s) => (
                  <option key={s} value={s}>
                    {s} {c.currency}
                  </option>
                ))}
              </select>
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
            {tr("plannerPrices.save")}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
