// web/lib/retailPayments.ts
// AA-1067 (crm-049 / crm-050) — the shapes and pure helpers of the retail payment features: the payment schedule
// with instalments (the builder's lines, Settings > Payments' template, the trip's live status), the traveller
// cancellation policy (Settings > Cancellation policy) and the charge of the cancel dialog. Amounts are decimal
// strings the API computed; nothing is re-computed here (only the typed input is checked before it is sent).

import type { Tone } from "@/app/_kit";
import { fmt } from "@/lib/i18n";

export type ScheduleKind = "deposit" | "instalment" | "balance";
export const SCHEDULE_KINDS: readonly ScheduleKind[] = ["deposit", "instalment", "balance"];
export type LiveStatus = "due" | "partial" | "overdue" | "paid" | "void";

/** One line of a builder version's plan (``view.schedule``). */
export interface ScheduleLine {
  kind: ScheduleKind;
  seq: number;
  percent: string | null;
  due_on: string | null;
  days_before: number | null;
}

/** A plan line with its amount (``view.totals.schedule``, the client page's ``totals.schedule``). */
export interface ScheduleAmount extends ScheduleLine {
  amount: string;
}

/** One line of the agency template (Settings > Payments). */
export interface TemplateLine {
  kind: ScheduleKind;
  percent?: string;
  due: { anchor: "accept" | "before_start"; days?: number };
}

export interface ScheduleSettings {
  template: TemplateLine[];
  is_default: boolean;
  aa_default: TemplateLine[];
}

export interface PolicyTier {
  days_before_min: number;
  charge_pct: string;
  min_charge_amount: string | null;
}

export interface CancellationPolicy {
  policy_id: string;
  name: string;
  version: number;
  status: "draft" | "published" | "retired";
  replaces_policy_id: string | null;
  currency: string | null;
  is_default: boolean;
  published_at: string | null;
  retired_at: string | null;
  updated_at: string | null;
  tiers: PolicyTier[];
  text: { en: string; vi: string } | null;
}

export interface PolicySummary {
  policy_id: string;
  name: string;
  version: number;
  currency: string | null;
  tiers: PolicyTier[];
  text: { en: string; vi: string } | null;
}

export interface PolicyListing {
  policies: CancellationPolicy[];
  aa_default: { name: string; tiers: PolicyTier[] };
}

export type ChargeMode = "policy" | "override" | "waive";

/** The cancel dialog's ``charge`` block (api/app/cancel_policy/service.py Assessment.view). */
export interface CancelCharge {
  currency: string | null;
  trip_total: string;
  days_before: number | null;
  policy: { policy_id: string; name: string; version: number; frozen: boolean } | null;
  tier: { days_before_min: number; charge_pct: string } | null;
  policy_charge: string | null;
  decision: "policy" | "override" | "waived" | "no_policy";
  charge_amount: string;
  reason: string | null;
  paid_amount: string;
  kept_amount: string;
  refund_due: string;
  invoice_amount: string;
  warnings: string[];
  needs_owner: boolean;
  aa_fee_amount?: string | null;
}

/** A row of the builder's line editor while the agent types (strings as typed). ``id`` is the row's React key: it
 *  stays with the row when an earlier one is removed, so each due-date field keeps its own date (never sent). */
export interface DraftLine {
  id: string;
  kind: ScheduleKind;
  percent: string;
  due_on: string;
  days_before: number | null;
}

const PERCENT = /^\d{1,3}(\.\d{1,2})?$/;
const ISO = /^\d{4}-\d{2}-\d{2}$/;
const MONEY = /^\d{1,12}(\.\d{1,2})?$/;

/** "Deposit", "Instalment 2", "Balance". */
export function lineLabel(line: Pick<ScheduleLine, "kind" | "seq">, tr: (key: string) => string): string {
  return line.kind === "instalment" ? fmt(tr("schedule.kind.instalment"), { n: line.seq }) : tr(`schedule.kind.${line.kind}`);
}

let draftSeq = 0;
const draftId = () => `line-${++draftSeq}`;

export function draftLines(lines: ScheduleLine[]): DraftLine[] {
  return lines.map((l) => ({ id: draftId(), kind: l.kind, percent: l.percent ?? "", due_on: l.due_on ?? "", days_before: l.days_before }));
}

/** Add an instalment before the balance (or at the end), 10 % and no date yet. */
export function addInstalment(rows: DraftLine[]): DraftLine[] {
  const at = rows.findIndex((r) => r.kind === "balance");
  const line: DraftLine = { id: draftId(), kind: "instalment", percent: "10", due_on: "", days_before: null };
  return at < 0 ? [...rows, line] : [...rows.slice(0, at), line, ...rows.slice(at)];
}

/** A percent as typed: 0.01 to 100 (the deposit from 1: the proposal's deposit percent mirrors it, 1 to 100). */
function pctOk(raw: string, min = 0.01): string | null {
  const v = raw.trim().replace(",", ".");
  return PERCENT.test(v) && Number(v) >= min && Number(v) <= 100 ? v : null;
}

/** The PATCH ``schedule`` body, or null when a line is not valid (the API checks the order and the sums again). A
 *  line keeping the template's days sends them (it follows a new day 1); a typed date (days cleared) is sent as is. */
export function scheduleBody(rows: DraftLine[]): Record<string, unknown>[] | null {
  if (!rows.length || rows[0].kind !== "deposit") return null;
  const out: Record<string, unknown>[] = [];
  for (const r of rows) {
    const line: Record<string, unknown> = { kind: r.kind };
    if (r.kind !== "balance") {
      const pct = pctOk(r.percent, r.kind === "deposit" ? 1 : 0.01);
      if (pct === null) return null;
      line.percent = pct;
    }
    if (r.kind !== "deposit") {
      if (r.days_before !== null) {
        line.days_before = r.days_before; // still follows the template: the API dates it from day 1
      } else if (r.due_on) {
        if (!ISO.test(r.due_on)) return null;
        line.due_on = r.due_on;
      }
    }
    out.push(line);
  }
  return out;
}

/** The percents typed: the deposit and the instalments; the balance takes the rest (shown, never sent). */
export function restPercent(rows: DraftLine[]): number {
  const used = rows.filter((r) => r.kind !== "balance").reduce((sum, r) => sum + (Number(r.percent.replace(",", ".")) || 0), 0);
  return Math.round((100 - used) * 100) / 100;
}

/** The PUT body of the template, or null when a line is not valid. */
export function templateBody(rows: { kind: ScheduleKind; percent: string; days: string }[]): TemplateLine[] | null {
  if (!rows.length || rows[0].kind !== "deposit") return null;
  const out: TemplateLine[] = [];
  for (const r of rows) {
    if (r.kind === "deposit") {
      const pct = pctOk(r.percent, 1);
      if (pct === null) return null;
      out.push({ kind: "deposit", percent: pct, due: { anchor: "accept" } });
      continue;
    }
    const days = Number(r.days.trim());
    if (!/^\d{1,3}$/.test(r.days.trim()) || days > 730) return null;
    const line: TemplateLine = { kind: r.kind, due: { anchor: "before_start", days } };
    if (r.kind === "instalment") {
      const pct = pctOk(r.percent);
      if (pct === null) return null;
      line.percent = pct;
    }
    out.push(line);
  }
  return out;
}

export function templateRows(lines: TemplateLine[]): { kind: ScheduleKind; percent: string; days: string }[] {
  return lines.map((l) => ({ kind: l.kind, percent: l.percent ?? "", days: l.due.days === undefined ? "" : String(l.due.days) }));
}

/** A template line in words: "30 % at acceptance", "40 % 60 days before departure", "the rest 30 days before". */
export function templateLineText(l: TemplateLine, tr: (key: string) => string): string {
  if (l.kind === "deposit") return fmt(tr("schedule.tpl.deposit"), { pct: Number(l.percent) });
  if (l.kind === "instalment") return fmt(tr("schedule.tpl.instalment"), { pct: Number(l.percent), n: l.due.days ?? 0 });
  return fmt(tr("schedule.tpl.balance"), { n: l.due.days ?? 0 });
}

export function liveTone(status: LiveStatus | string | undefined): Tone {
  switch (status) {
    case "paid":
      return "success";
    case "overdue":
      return "danger";
    case "partial":
      return "warning";
    case "void":
      return "neutral";
    default:
      return "info";
  }
}

/** The tier rows the policy editor holds (strings as typed). */
export interface DraftTier {
  days_before_min: string;
  charge_pct: string;
  min_charge_amount: string;
}

export function draftTiers(tiers: PolicyTier[]): DraftTier[] {
  return tiers.map((t) => ({ days_before_min: String(t.days_before_min), charge_pct: t.charge_pct, min_charge_amount: t.min_charge_amount ?? "" }));
}

/** The tiers body, or null when a row is not valid (days 0-730 once each, a 0-day row, percent 0-100). */
export function tiersBody(rows: DraftTier[]): { days_before_min: number; charge_pct: string; min_charge_amount?: string }[] | null {
  if (!rows.length || rows.length > 12) return null;
  const seen = new Set<number>();
  const out: { days_before_min: number; charge_pct: string; min_charge_amount?: string }[] = [];
  for (const r of rows) {
    const days = Number(r.days_before_min.trim());
    const pct = r.charge_pct.trim().replace(",", ".");
    if (!/^\d{1,3}$/.test(r.days_before_min.trim()) || days > 730 || seen.has(days)) return null;
    if (!PERCENT.test(pct) || Number(pct) > 100) return null;
    seen.add(days);
    const row: { days_before_min: number; charge_pct: string; min_charge_amount?: string } = { days_before_min: days, charge_pct: pct };
    const min = r.min_charge_amount.trim().replace(",", ".");
    if (min) {
      if (!MONEY.test(min) || Number(min) <= 0) return null;
      row.min_charge_amount = min;
    }
    out.push(row);
  }
  if (!seen.has(0)) return null;
  return out.sort((a, b) => b.days_before_min - a.days_before_min);
}

/** A tier in words for a list ("31 days or more: 10 %", "0 days: 100 %"). */
export function tierText(t: PolicyTier, tr: (key: string) => string): string {
  return fmt(tr("policy.tierLine"), { days: t.days_before_min, pct: Number(t.charge_pct) });
}

/** The cancel body's ``charge``: the policy's (absent), an override (amount + reason) or a waiver (reason); null when
 *  the typed amount or reason is missing. */
export function chargeBody(mode: ChargeMode, amount: string, reason: string): Record<string, string> | null | undefined {
  if (mode === "policy") return undefined;
  const why = reason.trim();
  if (!why || why.length > 500) return null;
  if (mode === "waive") return { mode, reason: why };
  const v = amount.trim().replace(/[\s,]/g, "");
  if (!MONEY.test(v)) return null;
  return { mode, amount: v, reason: why };
}

/** The words of a refusal code of these routes, or null for another code. */
export function paymentsErrorKey(detail: string): string | null {
  const known = new Set([
    "invalid_schedule",
    "invalid_tiers",
    "invalid_charge",
    "charge_reason_required",
    "cancel_charge_owner_only",
    "policy_published",
    "policy_is_draft",
    "policy_draft_exists",
    "policy_not_live",
    "policy_without_tiers",
    "currency_required",
    "too_many_policies",
    "no_starter_mail",
  ]);
  return known.has(detail) ? `payments.err.${detail}` : null;
}
