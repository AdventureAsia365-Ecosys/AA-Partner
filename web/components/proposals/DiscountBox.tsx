"use client";
// web/components/proposals/DiscountBox.tsx — AA-1066 (crm-048, item crm-07): the builder's discount. Owner / agent set
// a percent off the charged total (pay-on-arrival items are never discounted); above the agency limit (Settings >
// Pricing, default 10 %), or when the discounted total is below the net, an agent asks the owner, who approves or
// rejects it here (and from the task the request opens). An owner's own discount is recorded as approved when they
// publish. The API decides every rule; this box shows its state.
import { useState } from "react";
import { Badge, Button, Field, Input, K, useHydrated } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import { discountInput, type DiscountState } from "@/lib/retailPricing";

type Send = (method: "POST" | "PATCH", path: string, body?: Record<string, unknown>) => Promise<boolean>;

export function DiscountBox({
  proposalId,
  state,
  draft,
  canMoney,
  isOwner,
  busy,
  locale,
  tr,
  write,
  onMessage,
}: {
  proposalId: string;
  state: DiscountState;
  draft: boolean;
  canMoney: boolean;
  isOwner: boolean;
  busy: boolean;
  locale: DateLocale;
  tr: (key: string) => string;
  write: Send;
  onMessage: (tone: "danger" | "success", text: string) => void;
}) {
  const hydrated = useHydrated();
  const [value, setValue] = useState(state.discount_pct ? String(Number(state.discount_pct)) : "");
  const pending = state.pending;

  async function save() {
    const pct = discountInput(value);
    if (pct === undefined) {
      onMessage("danger", tr("pricing.err.invalid_discount_pct"));
      return;
    }
    if (await write("PATCH", `proposals/${proposalId}/builder`, { discount_pct: pct })) onMessage("success", tr("discount.saved"));
  }

  async function decide(decision: "approved" | "rejected") {
    if (!pending) return;
    if (await write("POST", `proposals/${proposalId}/discount-approval/${pending.discount_approval_id}`, { decision })) {
      onMessage("success", tr(`discount.decided.${decision}`));
    }
  }

  return (
    <div data-testid="builder-discount" data-needs-approval={state.needs_approval ? "true" : undefined} style={{ display: "grid", gap: 6, marginTop: 14 }}>
      <strong style={{ fontSize: 13 }}>{tr("discount.title")}</strong>
      <span style={{ fontSize: 12.5, color: K.muted }}>{fmt(tr("discount.limit"), { pct: Number(state.limit_pct) })}</span>
      {draft && canMoney ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
          style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}
        >
          <Field label={tr("discount.pct")}>
            <Input size="sm" inputMode="decimal" value={value} placeholder={tr("discount.none")} onChange={(e) => setValue(e.target.value)} style={{ width: 120 }} />
          </Field>
          <span data-testid="builder-discount-save">
            <Button type="submit" size="sm" disabled={!hydrated || busy}>
              {tr("discount.save")}
            </Button>
          </span>
        </form>
      ) : (
        <span style={{ fontSize: 13 }}>{state.discount_pct ? `${Number(state.discount_pct)}%` : tr("discount.none")}</span>
      )}
      {state.below_cost && (
        <span role="status" style={{ fontSize: 12.5, color: K.danger }}>
          {tr("discount.belowCost")}
        </span>
      )}
      {state.needs_approval && (
        <span style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <Badge tone={isOwner ? "warning" : "danger"}>{tr("discount.needsApproval")}</Badge>
          {isOwner && <span style={{ fontSize: 12.5, color: K.muted }}>{tr("discount.ownerNote")}</span>}
        </span>
      )}
      {state.approved_pct && !state.needs_approval && (state.above_limit || state.below_cost) && (
        <span style={{ fontSize: 12.5, color: K.success }}>{fmt(tr("discount.approvedUpTo"), { pct: Number(state.approved_pct) })}</span>
      )}
      {pending && (
        <span data-testid="builder-discount-pending" style={{ fontSize: 12.5, color: K.amber }}>
          {fmt(tr("discount.pending"), { pct: Number(pending.discount_pct), date: pending.requested_at ? formatDate(pending.requested_at.slice(0, 10), locale) : "" })}
        </span>
      )}
      {draft && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {state.needs_approval && !isOwner && canMoney && !pending && (
            <span data-testid="builder-discount-request">
              <Button
                size="sm"
                variant="primary"
                disabled={!hydrated || busy}
                onClick={async () => {
                  if (await write("POST", `proposals/${proposalId}/discount-approval`)) onMessage("success", tr("discount.requested"));
                }}
              >
                {tr("discount.request")}
              </Button>
            </span>
          )}
          {isOwner && pending && (
            <>
              <span data-testid="builder-discount-approve">
                <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={() => void decide("approved")}>
                  {tr("discount.approve")}
                </Button>
              </span>
              <Button size="sm" variant="ghost" disabled={!hydrated || busy} onClick={() => void decide("rejected")}>
                {tr("discount.reject")}
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
