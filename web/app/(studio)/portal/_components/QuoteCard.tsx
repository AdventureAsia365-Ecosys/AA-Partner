"use client";
// web/app/(studio)/portal/_components/QuoteCard.tsx — AA-1129 (S283 UI audit rows 18 and 24, S1 "Studio money"): the
// agency's monthly bill line by line (GET /v1/billing `quote`, contract tenant.entitlements.v1 1.9): every product, the
// seats above the plan, the founding / combo discount, a free pilot, the monthly total and the yearly price. Before,
// Billing and the Dashboard showed the amount due next to the plan fee alone ("$409.50" beside "$349 · Within plan").
// `compact` is the Dashboard's short form (lines, discount, total; no heading or yearly line). No quote lines (an
// older CIS): nothing renders and the pages keep the plan fee.
import { Card } from "@/app/_kit/local/studio/StudioCard";
import { K, useLang, useT, type Translator } from "@/app/_kit";
import { fmtCount } from "@/lib/studio/format";
import { type QuoteNote, type QuoteView } from "@/lib/studio/billingMoney";
import { QUOTE_MESSAGES } from "./account.messages";

type QuoteT = Translator<typeof QUOTE_MESSAGES.en>;

function noteText(n: QuoteNote, t: QuoteT, count: (v: number) => string): string {
  switch (n.kind) {
    case "quantity":
      return t("quantity", count(n.n));
    case "extraSeats":
      if (n.price) return n.n === 1 ? t("extraSeatAt", count(n.n), n.price) : t("extraSeatsAt", count(n.n), n.price);
      return n.n === 1 ? t("extraSeat", count(n.n)) : t("extraSeats", count(n.n));
    case "trial":
      return t("trial");
    case "agreed":
      return t("agreed");
    case "priceMissing":
      return t("priceMissing");
    case "yearly":
      return t("yearly");
  }
}

function discountLabel(kind: string | null, t: QuoteT): string {
  if (kind === "founding") return t("discountFounding");
  if (kind === "combo") return t("discountCombo");
  return t("discount");
}

function Row({ label, sub, amount, strong = false, testId }: { label: string; sub?: string; amount: string; strong?: boolean; testId?: string }) {
  return (
    <li
      data-testid={testId}
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "baseline",
        gap: 12,
        padding: "9px 0",
        borderTop: `1px solid ${K.line}`,
        fontSize: strong ? 14 : 13,
        fontWeight: strong ? 700 : 400,
        color: K.ink,
      }}
    >
      <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
        {label}
        {sub && <span style={{ display: "block", fontSize: 11.5, fontWeight: 400, color: K.muted, marginTop: 2 }}>{sub}</span>}
      </span>
      <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{amount}</span>
    </li>
  );
}

/** The bill's rows: one per product, then the subtotal and discount (when there is one), the pilot note and the total. */
export function QuoteLines({ quote, compact = false }: { quote: QuoteView; compact?: boolean }) {
  const t = useT(QUOTE_MESSAGES);
  const lang = useLang();
  const count = (v: number) => fmtCount(v, lang);
  const pct = (v: number) => v.toLocaleString(lang === "vi" ? "vi-VN" : "en-US", { maximumFractionDigits: 2 });
  return (
    <ul aria-label={t("tableLabel")} data-testid="quote-lines" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {quote.rows.map((r) => (
        <Row key={r.key} label={r.name} sub={r.notes.map((n) => noteText(n, t, count)).join(" · ") || undefined} amount={r.amount} testId="quote-line" />
      ))}
      {quote.discount && (
        <>
          {!compact && <Row label={t("subtotal")} amount={quote.subtotal} />}
          <Row
            label={`${discountLabel(quote.discount.kind, t)}${quote.discount.pct !== null ? t("pct", pct(quote.discount.pct)) : ""}`}
            amount={`− ${quote.discount.amount}`}
            testId="quote-discount"
          />
        </>
      )}
      {quote.pilotFree && <Row label={t("pilotFree")} amount="" testId="quote-pilot" />}
      <Row label={t("total")} amount={quote.total} strong testId="quote-total" />
    </ul>
  );
}

/** Billing: the monthly bill card. */
export function QuoteCard({ quote }: { quote: QuoteView | null }) {
  const t = useT(QUOTE_MESSAGES);
  const lang = useLang();
  if (!quote) return null;
  return (
    <Card style={{ marginBottom: 16 }}>
      <div data-testid="billing-quote">
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 4 }}>{t("title")}</div>
        <p style={{ fontSize: 12.5, color: K.muted, margin: "0 0 10px", lineHeight: 1.5 }}>{t("sub")}</p>
        <QuoteLines quote={quote} />
        {quote.annual && (
          <p data-testid="quote-annual" style={{ fontSize: 12, color: K.muted, margin: "10px 0 0" }}>
            {t("annual", fmtCount(quote.annual.months, lang), quote.annual.total)}
          </p>
        )}
      </div>
    </Card>
  );
}

/** The Dashboard's "This month" badge: overage, else "No overage" beside a bill (the amount is more than the plan
 *  fee), else "Within plan" (an older CIS without lines: the amount is the plan fee). */
export function spendBadgeKey(overage: number, bill: QuoteView | null): "includesOverage" | "noOverage" | "withinPlan" {
  if (overage > 0) return "includesOverage";
  return bill ? "noOverage" : "withinPlan";
}

/** Billing's "Amount due so far" note when there is no overage. */
export function noOverageKey(bill: QuoteView | null): "noOverageBill" | "noOverage" {
  return bill ? "noOverageBill" : "noOverage";
}

/** Dashboard: the bill's short form under the amount due; nothing without a bill. */
export function DashboardBill({ quote }: { quote: QuoteView | null }) {
  if (!quote) return null;
  return (
    <div data-testid="dashboard-quote" style={{ marginTop: 14 }}>
      <QuoteLines quote={quote} compact />
    </div>
  );
}
