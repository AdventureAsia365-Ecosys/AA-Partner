// web/components/proposals/ClientPaymentLines.tsx — AA-1067 (crm-050, items crm-08 / crm-11) on the traveller's
// proposal page: every payment line of a version with instalments (before the accept: the share of the client's
// current total and its date; after: the rows with their status), and the agency's cancellation policy that goes
// with the version (the charge table in the client's language). Words: client.* keys (lib/clientMessages.ts ships
// only those to the page).
import { K } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, langOf } from "@/lib/i18n";
import { formatAmount } from "@/lib/proposals";
import type { PolicySummary } from "@/lib/retailPayments";

type T = (k: string) => string;

export function clientLineLabel(kind: string, seq: number | undefined, t: T): string {
  return kind === "instalment" ? fmt(t("client.schedule.instalment"), { n: seq ?? 1 }) : t(`client.schedule.${kind}`);
}

/** The dt / dd pairs of the amounts list: one per line (before the accept). */
export function ClientPlanLines({
  lines,
  currency,
  locale,
  t,
}: {
  lines: { kind: string; seq: number; amount: string; due_on: string | null }[];
  currency: string;
  locale: DateLocale;
  t: T;
}) {
  return (
    <>
      {lines.map((l) => (
        <div key={`${l.kind}-${l.seq}`} data-testid="client-plan-line" data-kind={l.kind} style={{ display: "contents" }}>
          <dt style={{ color: K.muted }}>{clientLineLabel(l.kind, l.seq, t)}</dt>
          <dd style={{ margin: 0, color: K.ink }}>
            {formatAmount(l.amount, currency, locale)}
            {", "}
            {l.due_on ? fmt(t("client.schedule.due"), { date: formatDate(l.due_on, locale) }) : t("client.schedule.atAccept")}
          </dd>
        </div>
      ))}
    </>
  );
}

/** The accepted page's rows ("Instalment 1: 400.00 USD, due 1 Dec 2026"). */
export function ClientScheduleRows({
  rows,
  locale,
  t,
}: {
  rows: { kind: string; seq?: number; amount: string; currency: string; due_on: string; status: string }[];
  locale: DateLocale;
  t: T;
}) {
  return (
    <>
      {rows.map((s) => (
        <p key={`${s.kind}-${s.seq ?? 1}`} data-testid="client-schedule-row" data-kind={s.kind} style={{ margin: 0, fontSize: 13.5, color: K.body }}>
          {fmt(t(`client.scheduleRow.${s.status}`), {
            what: clientLineLabel(s.kind, s.seq, t),
            amount: formatAmount(s.amount, s.currency, locale),
            date: formatDate(s.due_on, locale),
          })}
        </p>
      ))}
    </>
  );
}

export function ClientPolicy({ policy, locale, t }: { policy: PolicySummary | null | undefined; locale: DateLocale; t: T }) {
  if (!policy?.text) return null;
  const text = policy.text[langOf(locale)] ?? policy.text.en;
  return (
    <section data-testid="client-cancellation-policy" style={{ display: "grid", gap: 4 }}>
      <strong style={{ fontSize: 14, color: K.ink }}>{t("client.policy.title")}</strong>
      <p style={{ margin: 0, fontSize: 13, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{text}</p>
    </section>
  );
}
