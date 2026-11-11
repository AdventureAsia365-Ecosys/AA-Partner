"use client";
// web/components/pipeline/StageDialogs.tsx — the dialogs of the stage engine (AA-943, CRM-15; design v3 §2.7 F2, §3.5,
// C12, Fix B8):
//
//   - GateDialog: a move the API refused with 409 `stage2_incomplete` / `gate_unmet` (Qualifying -> Proposal below
//     80 %, Proposal -> Booked, Booked -> Pre-departure). It shows the Stage 2 percentage and what is missing, and lets
//     the member go ahead with one of the fixed override reasons (logged on the trip's history and in the audit);
//   - CancelDialog: the cancel flow (C12, AA-957). A reason first, then the dry run lists every effect, Booking's fee
//     per AA booking, the agency's own supplier bookings to cancel, the refund per payment (default all that is left;
//     lower keeps the rest) under the accepted terms, and the expected commissions (tick the ones the supplier still
//     pays); the member types CANCEL to run it. A cancellation has no Undo. While AA has not confirmed it the dialog
//     shows the pending AA bookings and "Ask AA again";
//   - BulkDialog: a column's "move all": reasons when the target needs one, the dry run's eligible / blocked counts,
//     then the run with the preview's hash (409 `preview_stale` asks to check again).
//
// No personal data here: ids, counts and codes only.
import { useEffect, useState } from "react";
import { Button, Checkbox, Field, Input, K, Modal, Select } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, plural, type Lang, type MessageKey, t } from "@/lib/i18n";
import { aaCancelFailedKey } from "@/lib/aa";
import {
  CANCEL_REASONS,
  codeLabel,
  gateOf,
  LOST_REASONS,
  moveErrorKey,
  OVERRIDE_REASONS,
  percentText,
  stageLabel,
  type BoardStage,
  type BulkPreview,
  type CancelPreview,
  type GateRefusal,
  type Stage,
} from "@/lib/pipeline";
import { formatAmount } from "@/lib/proposals";
import { chargeBody, paymentsErrorKey, type CancelCharge, type ChargeMode } from "@/lib/retailPayments";

export const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  maxWidth: "100%",
};

/** Missing field codes worded ("Diet, Trip style"): Stage 1 codes under s1.*, Stage 2 codes under plan.field.*. */
export function missingLabel(lang: Lang, codes: string[], prefix: "s1" | "plan.field" = "plan.field"): string {
  return codes.map((c) => codeLabel(lang, prefix, c)).join(", ");
}

/** One effect line of the cancel dry run; a kind a later card adds before this web words it shows as "kind: n". */
export function effectLabel(lang: Lang, kind: string, n: number): string {
  const text = t(lang, `cancelTrip.effect.${kind}` as MessageKey) ?? t(lang, "cancelTrip.effect.other");
  return fmt(text, { n, kind });
}

/** The dialog box of the stage engine: the kit Modal's pinned-footer layout (AA-1008 moved the S262 frame into the kit,
 * root ADR 0016). S262 (AA-957 live, 390 px and 1440 px): the title and the `footer` (the confirm step and the buttons)
 * stay in view; only the body between them scrolls, with a "more below" cue while it overflows (one scroll area, never
 * one nested in another). On a window under 480 px tall (a phone held sideways) the whole box scrolls instead and the
 * footer is compact. The scrolling body is "<testId>-scroll" (S262 round 3: "-body" clashed with GateDialog's own
 * "gate-dialog-body" paragraph, which the E2E and UAT scenarios read). The first control is focused on open. */
export function Dialog({
  title,
  onClose,
  children,
  footer,
  moreLabel,
  testId,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  /** Kept in view under the scrolling body: the confirm step and the buttons. */
  footer?: React.ReactNode;
  /** The scroll cue's words ("More below"); no cue without them. */
  moreLabel?: string;
  testId: string;
}) {
  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      width={480}
      testId={testId}
      bodyTestId={`${testId}-scroll`}
      footer={footer}
      moreLabel={moreLabel}
      pinFooter
      initialFocus
    >
      <div style={{ display: "grid", gap: 12 }}>{children}</div>
    </Modal>
  );
}

function Actions({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "flex-end" }}>{children}</div>;
}

function Problem({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
      {text}
    </p>
  );
}

export function GateDialog({
  refusal,
  lang,
  onConfirm,
  onClose,
  onFill,
}: {
  refusal: GateRefusal;
  lang: Lang;
  onConfirm: (reason: string) => Promise<void> | void;
  onClose: () => void;
  /** Offered on the Stage 2 gate when the trip page can open "Fill in from call". */
  onFill?: () => void;
}) {
  const [reason, setReason] = useState(refusal.override_reasons.length === 1 ? refusal.override_reasons[0] : "");
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      title={t(lang, `gate.title.${refusal.gate}` as MessageKey)}
      onClose={onClose}
      testId="gate-dialog"
      footer={
        <Actions>
          {onFill && refusal.gate === "stage2" && (
            <Button size="sm" variant="ghost" onClick={onFill}>
              {t(lang, "gate.fill")}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onClose}>
            {t(lang, "trip.cancel")}
          </Button>
          <span data-testid="gate-confirm" style={{ display: "contents" }}>
            <Button
              size="sm"
              variant="primary"
              disabled={!reason || busy}
              onClick={async () => {
                setBusy(true);
                await onConfirm(reason);
                setBusy(false);
              }}
            >
              {t(lang, "gate.confirm")}
            </Button>
          </span>
        </Actions>
      }
    >
      <p style={{ margin: 0, fontSize: 13.5, color: K.body }} data-testid="gate-dialog-body" data-gate={refusal.gate} data-s2={refusal.s2 ?? ""}>
        {fmt(t(lang, `gate.body.${refusal.gate}` as MessageKey), { n: refusal.s2 ?? 0 })}
      </p>
      {refusal.missing.length > 0 && (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="gate-missing">
          {fmt(t(lang, "gate.missing"), { list: missingLabel(lang, refusal.missing) })}
        </p>
      )}
      <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
        {t(lang, "gate.reason")}
        <select value={reason} onChange={(e) => setReason(e.target.value)} style={field} data-testid="gate-reason">
          <option value="">{t(lang, "plan.choose")}</option>
          {refusal.override_reasons.map((r) => (
            <option key={r} value={r}>
              {codeLabel(lang, "override", r)}
            </option>
          ))}
        </select>
      </label>
    </Dialog>
  );
}

export function CancelDialog({
  tripId,
  lang,
  locale,
  onDone,
  onClose,
}: {
  tripId: string;
  lang: Lang;
  locale: DateLocale;
  onDone: (result: CancelPreview) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<CancelPreview | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // AA-957: the refund asked back per payment (default: all that is left) and the commissions the supplier still pays.
  const [refunds, setRefunds] = useState<Record<string, string>>({});
  const [keep, setKeep] = useState<Record<string, boolean>>({});
  // AA-1071: keep the sales partner's commission (the partner is still paid).
  const [keepPartner, setKeepPartner] = useState(false);
  // AA-1067: the charge to the client (the policy's, another amount with a reason, or waived with a reason).
  const [mode, setMode] = useState<ChargeMode>("policy");
  const [amount, setAmount] = useState("");
  const [why, setWhy] = useState("");
  // The charge the figures on screen were worked out with (chargeKey of the last dry run). Changing the mode, the
  // amount or the reason makes them stale: Confirm waits for "Update the figures", so the run never sends a charge
  // with refunds prefilled for another one.
  const [checked, setChecked] = useState<string | null>(null);
  const tr = (key: string) => t(lang, key as MessageKey);
  const pending = preview?.cancellation?.status === "pending";

  /** The charge part of a body: undefined = the policy's; null = the typed amount or reason is missing. */
  function chargePart(): Record<string, string> | null | undefined {
    return chargeBody(mode, amount, why);
  }

  const stale = Boolean(preview && !pending && preview.charge && chargeKey(chargePart()) !== checked);

  async function dryRun(code: string, charge: Record<string, string> | undefined = undefined) {
    setReason(code);
    setPreview(null);
    setError(null);
    setChecked(null);
    if (!code) return;
    setBusy(true);
    const res = await bffSend<CancelPreview>("POST", `trips/${tripId}/cancel?dry_run=true`, { cancel_reason_code: code, ...(charge ? { charge } : {}) });
    setBusy(false);
    if (!res.ok) {
      setError(t(lang, moveErrorKey(res.detail)));
      return;
    }
    setPreview(res.data);
    setChecked(chargeKey(charge));
    setRefunds(Object.fromEntries((res.data.payments ?? []).filter((p) => !p.needs_invoice).map((p) => [p.payment_id, p.refund_amount])));
    setKeep({});
    setKeepPartner(false);
  }

  /** Another reason: a new dry run with the charge as set now (the policy's when the typed one is not complete). */
  async function chooseReason(code: string) {
    await dryRun(code, chargePart() ?? undefined);
  }

  async function recheck() {
    const charge = chargePart();
    if (charge === null) {
      setError(tr("cancelCharge.invalid"));
      return;
    }
    await dryRun(reason, charge);
  }

  async function run() {
    const charge = chargePart();
    if (charge === null) {
      setError(tr("cancelCharge.invalid"));
      return;
    }
    if (stale) {
      setError(tr("cancelCharge.stale"));
      return;
    }
    setBusy(true);
    setError(null);
    const body = {
      cancel_reason_code: reason,
      refunds: Object.entries(refunds).map(([payment_id, amount]) => ({ payment_id, amount: amount.trim() || "0" })),
      keep_commission: Object.entries(keep).filter(([, v]) => v).map(([id]) => id),
      keep_partner_commission: keepPartner,
      ...(charge ? { charge } : {}),
    };
    const res = await bffSend<CancelPreview>("POST", `trips/${tripId}/cancel?dry_run=false`, body);
    setBusy(false);
    if (res.ok) onDone(res.data);
    else setError(paymentsErrorKey(res.detail) ? tr(paymentsErrorKey(res.detail) as string) : t(lang, moveErrorKey(res.detail)));
  }

  const confirmStep = preview && !pending;
  return (
    <Dialog
      title={t(lang, pending ? "cancelTrip.pendingTitle" : "cancelTrip.title")}
      onClose={onClose}
      testId="cancel-dialog"
      moreLabel={tr("cancelTrip.moreBelow")}
      footer={
        <>
          {confirmStep && (
            <>
              <p style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{t(lang, "cancelTrip.noUndo")}</p>
              <Field label={t(lang, "cancelTrip.typeToConfirm")}>
                <Input size="sm" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" data-testid="cancel-typed" />
              </Field>
            </>
          )}
          <Problem text={error} />
          <Actions>
            <Button size="sm" variant="ghost" onClick={onClose}>
              {t(lang, "cancelTrip.keep")}
            </Button>
            {pending ? (
              <span data-testid="cancel-retry" style={{ display: "contents" }}>
                <Button size="sm" disabled={busy} onClick={() => void run()}>
                  {tr("cancelTrip.retry")}
                </Button>
              </span>
            ) : (
              <span data-testid="cancel-confirm" style={{ display: "contents" }}>
                <Button size="sm" variant="danger" disabled={!preview || stale || typed.trim() !== "CANCEL" || busy} onClick={() => void run()}>
                  {t(lang, "cancelTrip.confirm")}
                </Button>
              </span>
            )}
          </Actions>
        </>
      }
    >
      <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
        {t(lang, "cancelTrip.reason")}
        <select value={reason} onChange={(e) => void chooseReason(e.target.value)} style={field} data-testid="cancel-reason">
          <option value="">{t(lang, "plan.choose")}</option>
          {CANCEL_REASONS.map((r) => (
            <option key={r} value={r}>
              {codeLabel(lang, "cancel", r)}
            </option>
          ))}
        </select>
      </label>
      {busy && !preview && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "cancelTrip.checking")}</p>}
      {preview && (
        <div data-testid="cancel-effects" data-dry-run={String(preview.dry_run)} data-pending={pending ? "true" : undefined} style={{ display: "grid", gap: 10 }}>
          {!pending && (
            <>
              <span style={{ fontSize: 13, color: K.body }}>{t(lang, "cancelTrip.effects")}</span>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.body, display: "grid", gap: 2 }}>
                {preview.effects
                  .filter((e) => e.count > 0)
                  .map((e) => (
                    <li key={e.kind} data-kind={e.kind} data-count={e.count}>
                      {effectLabel(lang, e.kind, e.count)}
                    </li>
                  ))}
              </ul>
            </>
          )}
          {(preview.aa_bookings ?? []).length > 0 && (
            <section data-testid="cancel-aa-bookings" style={{ display: "grid", gap: 4, fontSize: 13 }}>
              <strong style={{ fontSize: 12.5 }}>{tr("cancelTrip.aaTitle")}</strong>
              {(preview.aa_bookings ?? []).map((b) => (
                <div key={b.booking_id} data-booking={b.booking_id} style={{ color: K.body }}>
                  <span style={{ overflowWrap: "anywhere" }}>{b.titles.join(", ")}</span>
                  <span style={{ display: "block", fontSize: 12, color: K.muted }} data-testid="cancel-aa-fee">
                    {b.fee
                      ? plural(lang, "cancelTrip.aaFee", Number(b.fee.days_before_departure), { amount: formatAmount(b.fee.amount, b.fee.currency, locale), pct: percentText(b.fee.fee_pct, locale), days: b.fee.days_before_departure })
                      : pending && b.failed_code
                        ? fmt(tr("cancelTrip.bookingFailed"), { reason: tr(aaCancelFailedKey(b.failed_code)) })
                        : pending
                          ? tr("cancelTrip.bookingAsked")
                          : tr("cancelTrip.aaFeeUnknown")}
                  </span>
                </div>
              ))}
              {preview.booking_unavailable && !pending && (
                <p style={{ margin: 0, fontSize: 12.5, color: K.amber }} data-testid="cancel-booking-down">
                  {tr("cancelTrip.bookingDown")}
                </p>
              )}
            </section>
          )}
          {!pending && (preview.supplier_lines ?? []).length > 0 && (
            <section data-testid="cancel-supplier-lines" style={{ display: "grid", gap: 2, fontSize: 13 }}>
              <strong style={{ fontSize: 12.5 }}>{tr("cancelTrip.suppliersTitle")}</strong>
              {(preview.supplier_lines ?? []).map((l) => (
                <span key={l.booking_line_id} style={{ overflowWrap: "anywhere" }}>
                  {l.supplier_name}: {l.title}
                  {l.confirmation_no ? ` (${l.confirmation_no})` : ""}
                </span>
              ))}
            </section>
          )}
          {!pending && preview.charge && (
            <CancelChargeBox
              charge={preview.charge}
              locale={locale}
              tr={tr}
              mode={mode}
              amount={amount}
              why={why}
              busy={busy}
              stale={stale}
              onMode={setMode}
              onAmount={setAmount}
              onWhy={setWhy}
              onRecheck={() => void recheck()}
            />
          )}
          {!pending && (preview.payments ?? []).length > 0 && (
            <section data-testid="cancel-refunds" style={{ display: "grid", gap: 6, fontSize: 13 }}>
              <strong style={{ fontSize: 12.5 }}>{tr("cancelTrip.refundsTitle")}</strong>
              <span style={{ fontSize: 12, color: K.muted }}>
                {preview.terms
                  ? fmt(tr("cancelTrip.terms"), { label: preview.terms.version_label, date: formatDate(preview.terms.accepted_at.slice(0, 10), locale) })
                  : tr("cancelTrip.noTerms")}
              </span>
              <span style={{ fontSize: 12, color: K.muted }}>{tr("cancelTrip.refundHelp")}</span>
              {(preview.payments ?? []).map((p) => (
                <div key={p.payment_id} data-payment={p.payment_id} style={{ display: "grid", gap: 4 }}>
                  <span>
                    {p.invoice_number
                      ? fmt(tr("cancelTrip.refundFor"), {
                          invoice: p.invoice_number,
                          amount: formatAmount(p.amount, p.currency, locale),
                          date: formatDate(p.received_on, locale),
                        })
                      : fmt(tr("cancelTrip.refundNoInvoice"), { amount: formatAmount(p.amount, p.currency, locale), date: formatDate(p.received_on, locale) })}
                  </span>
                  {p.needs_invoice ? (
                    <span style={{ fontSize: 12, color: K.amber }}>{tr("cancelTrip.needsInvoice")}</span>
                  ) : (
                    <Field label={fmt(tr("cancelTrip.refundAmount"), { currency: p.currency })} hint={fmt(tr("cancelTrip.refundMax"), { amount: formatAmount(p.refundable, p.currency, locale) })}>
                      <Input
                        size="sm"
                        inputMode="decimal"
                        data-testid="cancel-refund-amount"
                        value={refunds[p.payment_id] ?? ""}
                        onChange={(e) => setRefunds({ ...refunds, [p.payment_id]: e.target.value })}
                      />
                    </Field>
                  )}
                </div>
              ))}
            </section>
          )}
          {!pending && (preview.commissions ?? []).some((c) => c.status === "expected") && (
            <section data-testid="cancel-commissions" style={{ display: "grid", gap: 4, fontSize: 13 }}>
              <strong style={{ fontSize: 12.5 }}>{tr("cancelTrip.commissionsTitle")}</strong>
              {(preview.commissions ?? [])
                .filter((c) => c.status === "expected")
                .map((c) => (
                  <div key={c.commission_id} style={{ display: "grid", gap: 2 }}>
                    <span style={{ overflowWrap: "anywhere" }}>
                      {c.supplier_name}: {c.title}
                      {c.expected ? ` · ${formatAmount(c.expected.amount, c.expected.currency, locale)}` : ""}
                    </span>
                    <Checkbox
                      label={tr("cancelTrip.keepCommission")}
                      checked={Boolean(keep[c.commission_id])}
                      onChange={(e) => setKeep({ ...keep, [c.commission_id]: e.target.checked })}
                    />
                  </div>
                ))}
            </section>
          )}
          {!pending && (preview.partner_commissions ?? []).length > 0 && (
            <section data-testid="cancel-partner-commissions" style={{ display: "grid", gap: 4, fontSize: 13 }}>
              <span style={{ overflowWrap: "anywhere" }}>
                {(preview.partner_commissions ?? []).map((c) => formatAmount(c.amount, c.currency, locale)).join(" · ")}
              </span>
              <Checkbox label={tr("cancelTrip.keepPartnerCommission")} checked={keepPartner} onChange={(e) => setKeepPartner(e.target.checked)} />
            </section>
          )}
          {!pending && (preview.aa_bookings ?? []).length > 0 && (
            // Before the action (S262): the future, not the pending badge's "You cancelled this trip".
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="cancel-will-wait">
              {t(lang, "cancelTrip.willWaitHint")}
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}

/** The charge a dry run was worked out with, as a comparable key ("policy" for the policy's own charge). */
function chargeKey(charge: Record<string, string> | null | undefined): string {
  return charge === undefined ? "policy" : charge === null ? "invalid" : JSON.stringify(charge);
}

/** AA-1067 (crm-11 / crm-12): the charge block of the cancel dialog: days before departure, the frozen policy and its
 *  tier, the charge, what was paid, kept and is refunded, the fee invoice; the member may charge another amount or
 *  waive it (a reason each; an agent lowering it past the refund threshold needs the owner). */
function CancelChargeBox({
  charge,
  locale,
  tr,
  mode,
  amount,
  why,
  busy,
  stale,
  onMode,
  onAmount,
  onWhy,
  onRecheck,
}: {
  charge: CancelCharge;
  locale: DateLocale;
  tr: (key: string) => string;
  mode: ChargeMode;
  amount: string;
  why: string;
  busy: boolean;
  stale: boolean;
  onMode: (m: ChargeMode) => void;
  onAmount: (v: string) => void;
  onWhy: (v: string) => void;
  onRecheck: () => void;
}) {
  const cur = charge.currency ?? "";
  const money = (v: string | null | undefined) => formatAmount(v ?? "0", cur, locale);
  return (
    <section data-testid="cancel-charge" data-decision={charge.decision} style={{ display: "grid", gap: 6, fontSize: 13 }}>
      <strong style={{ fontSize: 12.5 }}>{tr("cancelCharge.title")}</strong>
      <span>{charge.days_before === null ? tr("cancelCharge.daysNone") : fmt(tr("cancelCharge.days"), { n: Math.max(charge.days_before, 0) })}</span>
      {charge.policy && charge.tier ? (
        <span>{fmt(tr("cancelCharge.policy"), { name: charge.policy.name, n: charge.policy.version, pct: Number(charge.tier.charge_pct) })}</span>
      ) : (
        <span style={{ color: K.muted }}>{tr("cancelCharge.noPolicy")}</span>
      )}
      {charge.policy_charge !== null && <span>{fmt(tr("cancelCharge.policyCharge"), { amount: money(charge.policy_charge) })}</span>}
      <strong data-testid="cancel-charge-amount">{fmt(tr("cancelCharge.charge"), { amount: money(charge.charge_amount) })}</strong>
      <span>{fmt(tr("cancelCharge.paid"), { amount: money(charge.paid_amount) })}</span>
      <span>{fmt(tr("cancelCharge.kept"), { amount: money(charge.kept_amount) })}</span>
      <span data-testid="cancel-refund-due">{fmt(tr("cancelCharge.refundDue"), { amount: money(charge.refund_due) })}</span>
      {Number(charge.invoice_amount) > 0 && <span data-testid="cancel-fee-invoice">{fmt(tr("cancelCharge.invoice"), { amount: money(charge.invoice_amount) })}</span>}
      {charge.aa_fee_amount && <span>{fmt(tr("cancelCharge.aaFee"), { amount: money(charge.aa_fee_amount) })}</span>}
      {charge.warnings.map((w) => (
        <span key={w} data-testid="cancel-charge-warning" data-code={w} style={{ color: w === "charge_below_aa_fee" ? K.danger : K.amber }}>
          {tr(`cancelCharge.warn.${w}`)}
        </span>
      ))}
      {charge.currency && (
        <>
          <Field label={tr("cancelCharge.mode")}>
            <Select
              size="sm"
              value={mode}
              data-testid="cancel-charge-mode"
              onChange={(e) => onMode(e.target.value as ChargeMode)}
              options={(["policy", "override", "waive"] as const).map((m) => ({ value: m, label: tr(`cancelCharge.mode.${m}`) }))}
            />
          </Field>
          {mode === "override" && (
            <Field label={fmt(tr("cancelCharge.amount"), { currency: cur })}>
              <Input size="sm" inputMode="decimal" value={amount} onChange={(e) => onAmount(e.target.value)} data-testid="cancel-charge-input" />
            </Field>
          )}
          {mode !== "policy" && (
            <Field label={tr("cancelCharge.reason")}>
              <Input size="sm" value={why} maxLength={500} onChange={(e) => onWhy(e.target.value)} data-testid="cancel-charge-reason" />
            </Field>
          )}
          <span data-testid="cancel-charge-update">
            <Button size="sm" variant={stale ? "primary" : "secondary"} disabled={busy} onClick={onRecheck}>
              {tr("cancelCharge.update")}
            </Button>
          </span>
          {stale && (
            <span data-testid="cancel-charge-stale" style={{ color: K.amber }}>
              {tr("cancelCharge.stale")}
            </span>
          )}
        </>
      )}
      {charge.needs_owner && (
        <span data-testid="cancel-charge-owner" style={{ color: K.danger }}>
          {tr("cancelCharge.needsOwner")}
        </span>
      )}
    </section>
  );
}

export interface BulkTarget {
  from: BoardStage;
  to: Stage;
  scope: { from_stage: BoardStage } | { trip_ids: string[] };
}

export function BulkDialog({
  target,
  lang,
  filtered,
  onDone,
  onClose,
}: {
  target: BulkTarget;
  lang: Lang;
  /** The board has a filter on: only the cards on screen move. */
  filtered: boolean;
  onDone: (result: BulkPreview) => void;
  onClose: () => void;
}) {
  const gate = gateOf(target.from, target.to);
  const needsReason = target.to === "lost" || gate === "booking" || gate === "stage3";
  const [lost, setLost] = useState("");
  const [override, setOverride] = useState(gate && OVERRIDE_REASONS[gate].length === 1 ? OVERRIDE_REASONS[gate][0] : "");
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  /** Both reasons a move to this stage needs are given (or it needs none): only then is the preview asked. */
  const reasonGiven = (l: string, o: string) => !needsReason || !!(target.to === "lost" ? l : o);
  const [busy, setBusy] = useState(() => reasonGiven(lost, override));
  const [error, setError] = useState<string | null>(null);

  function body(dryRun: boolean, hash?: string, l = lost, o = override) {
    return {
      ...target.scope,
      to: target.to,
      lost_reason: target.to === "lost" ? l || null : null,
      override_reason: gate ? o || null : null,
      dry_run: dryRun,
      ...(hash ? { preview_hash: hash } : {}),
    };
  }

  const askPreview = (l: string, o: string) => bffSend<BulkPreview>("POST", "pipeline/bulk-stage", body(true, undefined, l, o));

  function showPreview(res: Awaited<ReturnType<typeof askPreview>>) {
    setBusy(false);
    if (res.ok) setPreview(res.data);
    else {
      setPreview(null);
      setError(t(lang, moveErrorKey(res.detail)));
    }
  }

  async function check(l = lost, o = override) {
    setBusy(true);
    setError(null);
    showPreview(await askPreview(l, o));
  }

  // The preview follows the reasons: without one where it is needed, nothing is asked yet. Run where a reason changes
  // (its select), not in an effect after the render (react-hooks/set-state-in-effect).
  function reasonsChanged(l: string, o: string) {
    setPreview(null);
    if (reasonGiven(l, o)) void check(l, o);
  }

  // The first preview, when the dialog opens with every reason it needs (an answer after a close is dropped).
  useEffect(() => {
    if (!reasonGiven(lost, override)) return;
    let live = true;
    void askPreview(lost, override).then((res) => {
      if (live) showPreview(res);
    });
    return () => {
      live = false;
    };
  }, []);

  const stage = stageLabel(lang, target.to);
  const blockedCodes = preview ? Object.entries(preview.blocked.reduce<Record<string, number>>((m, b) => ({ ...m, [b.code]: (m[b.code] ?? 0) + 1 }), {})) : [];
  return (
    <Dialog
      title={fmt(t(lang, "pipeline.bulkTitle"), { stage })}
      onClose={onClose}
      testId="bulk-dialog"
      footer={
        <>
          <Problem text={error} />
          <Actions>
            <Button size="sm" variant="ghost" onClick={onClose}>
              {t(lang, "trip.cancel")}
            </Button>
            <span data-testid="bulk-confirm" style={{ display: "contents" }}>
              <Button
                size="sm"
                variant="primary"
                disabled={!preview || preview.eligible.length === 0 || busy}
                onClick={async () => {
                  if (!preview) return;
                  setBusy(true);
                  setError(null);
                  const res = await bffSend<BulkPreview>("POST", "pipeline/bulk-stage", body(false, preview.preview_hash));
                  setBusy(false);
                  if (res.ok) onDone(res.data);
                  else if (res.detail === "preview_stale") {
                    setError(t(lang, "move.error.preview_stale"));
                    void check();
                  } else setError(t(lang, moveErrorKey(res.detail)));
                }}
              >
                {plural(lang, "pipeline.bulkConfirm", preview?.eligible.length ?? 0)}
              </Button>
            </span>
          </Actions>
        </>
      }
    >
      {filtered && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{t(lang, "pipeline.bulkFiltered")}</p>}
      {target.to === "lost" && (
        <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
          {t(lang, "trip.lostReason")}
          <select value={lost} onChange={(e) => {
            setLost(e.target.value);
            reasonsChanged(e.target.value, override);
          }} style={field} data-testid="bulk-lost-reason">
            <option value="">{t(lang, "plan.choose")}</option>
            {LOST_REASONS.map((r) => (
              <option key={r} value={r}>
                {codeLabel(lang, "lost", r)}
              </option>
            ))}
          </select>
        </label>
      )}
      {gate && (
        <label style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
          {t(lang, gate === "stage2" ? "pipeline.overrideOptional" : "pipeline.overrideRequired")}
          <select value={override} onChange={(e) => {
            setOverride(e.target.value);
            reasonsChanged(lost, e.target.value);
          }} style={field} data-testid="bulk-override">
            <option value="">{t(lang, gate === "stage2" ? "pipeline.noReason" : "plan.choose")}</option>
            {OVERRIDE_REASONS[gate].map((r) => (
              <option key={r} value={r}>
                {codeLabel(lang, "override", r)}
              </option>
            ))}
          </select>
        </label>
      )}
      {busy && !preview && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "pipeline.bulkPreviewing")}</p>}
      {preview && (
        <div data-testid="bulk-preview" data-eligible={preview.eligible.length} data-blocked={preview.blocked.length} style={{ display: "grid", gap: 4, fontSize: 13, color: K.body }}>
          <span>{plural(lang, "pipeline.bulkPreview", preview.eligible.length, { stage })}</span>
          {preview.blocked.length > 0 && (
            <>
              <span>{plural(lang, "pipeline.bulkBlocked", preview.blocked.length)}</span>
              <ul style={{ margin: 0, paddingLeft: 18, color: K.muted }}>
                {blockedCodes.map(([code, n]) => (
                  <li key={code}>
                    {codeLabel(lang, "blocked", code)} · {n}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </Dialog>
  );
}
