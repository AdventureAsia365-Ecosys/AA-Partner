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
import { useEffect, useRef, useState } from "react";
import { Button, Checkbox, Field, Input, K } from "@/app/_kit";
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

/** The dialog box of the stage engine. S262 (AA-957 live, 390 px and 1440 px): the title and the `footer` (the
 * confirm step and the buttons) stay in view; only the body between them scrolls, with a "more below" cue while it
 * overflows (one scroll area, never one nested in another). On a window under 480 px tall (a phone held sideways) the
 * whole box scrolls instead and the footer is compact, so the body is never squeezed to a sliver (app/globals.css). */
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
  const box = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [more, setMore] = useState(false);
  useEffect(() => {
    box.current?.querySelector<HTMLElement>("select, input, textarea, button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // Focus once on open (the Escape handler keeps the onClose of the first render: the parent's close is stable).
  }, []);
  useEffect(() => {
    // The cue follows the body: measured when it scrolls or when it or its content changes size (the observer also
    // reports once on observe, so nothing is set during the effect itself). On a short window the whole box scrolls
    // instead (app/globals.css), so the box counts too: the cue says the confirm step is further down.
    const el = body.current;
    const frame = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const below = (node: HTMLElement | null) => !!node && node.scrollHeight - node.scrollTop - node.clientHeight > 4;
    const measure = () => setMore(below(el) || below(frame));
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    if (frame) observer.observe(frame);
    if (content.current) observer.observe(content.current);
    el.addEventListener("scroll", measure, { passive: true });
    frame?.addEventListener("scroll", measure, { passive: true });
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", measure);
      frame?.removeEventListener("scroll", measure);
    };
  }, []);
  return (
    <div
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      style={{ position: "fixed", inset: 0, background: "rgba(20, 16, 10, 0.45)", display: "grid", placeItems: "center", padding: 16, zIndex: 50 }}
    >
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-testid={testId}
        // The frame's layout (max height with its 90vh fallback, the scrolling body, the short-window mode) is CSS in
        // app/globals.css ("Stage-engine dialog frame"): an inline style cannot carry a fallback declaration or a
        // media query.
        data-crm-dialog="box"
        style={{ background: K.card, color: K.ink, borderRadius: 12, border: `1px solid ${K.line}`, width: "min(480px, 100%)" }}
      >
        <h2 style={{ margin: 0, padding: "18px 18px 0", fontSize: 16, fontWeight: 600, flex: "none" }}>{title}</h2>
        {/* The scrolling body is "<testId>-scroll" (S262 round 3): "-body" clashed with GateDialog's own
            "gate-dialog-body" paragraph, which the E2E and UAT scenarios read. */}
        <div ref={body} data-testid={`${testId}-scroll`} data-crm-dialog="body" style={{ padding: "12px 18px", position: "relative" }}>
          <div ref={content} style={{ display: "grid", gap: 12 }}>
            {children}
          </div>
          {more && moreLabel && (
            <div
              aria-hidden="true"
              data-testid="dialog-more"
              style={{
                position: "sticky",
                bottom: -12,
                marginTop: 4,
                padding: "18px 0 4px",
                textAlign: "center",
                fontSize: 12,
                color: K.muted,
                background: `linear-gradient(to bottom, transparent, ${K.card} 60%)`,
                pointerEvents: "none",
              }}
            >
              ↓ {moreLabel}
            </div>
          )}
        </div>
        {footer && (
          <div data-crm-dialog="footer" style={{ borderTop: `1px solid ${K.line}` }}>{footer}</div>
        )}
      </div>
    </div>
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
  const tr = (key: string) => t(lang, key as MessageKey);
  const pending = preview?.cancellation?.status === "pending";

  async function dryRun(code: string) {
    setReason(code);
    setPreview(null);
    setError(null);
    if (!code) return;
    setBusy(true);
    const res = await bffSend<CancelPreview>("POST", `trips/${tripId}/cancel?dry_run=true`, { cancel_reason_code: code });
    setBusy(false);
    if (!res.ok) {
      setError(t(lang, moveErrorKey(res.detail)));
      return;
    }
    setPreview(res.data);
    setRefunds(Object.fromEntries((res.data.payments ?? []).filter((p) => !p.needs_invoice).map((p) => [p.payment_id, p.refund_amount])));
    setKeep({});
  }

  async function run() {
    setBusy(true);
    setError(null);
    const body = {
      cancel_reason_code: reason,
      refunds: Object.entries(refunds).map(([payment_id, amount]) => ({ payment_id, amount: amount.trim() || "0" })),
      keep_commission: Object.entries(keep).filter(([, v]) => v).map(([id]) => id),
    };
    const res = await bffSend<CancelPreview>("POST", `trips/${tripId}/cancel?dry_run=false`, body);
    setBusy(false);
    if (res.ok) onDone(res.data);
    else setError(t(lang, moveErrorKey(res.detail)));
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
                <Button size="sm" variant="danger" disabled={!preview || typed.trim() !== "CANCEL" || busy} onClick={() => void run()}>
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
        <select value={reason} onChange={(e) => void dryRun(e.target.value)} style={field} data-testid="cancel-reason">
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function body(dryRun: boolean, hash?: string) {
    return {
      ...target.scope,
      to: target.to,
      lost_reason: target.to === "lost" ? lost || null : null,
      override_reason: gate ? override || null : null,
      dry_run: dryRun,
      ...(hash ? { preview_hash: hash } : {}),
    };
  }

  async function check() {
    setBusy(true);
    setError(null);
    const res = await bffSend<BulkPreview>("POST", "pipeline/bulk-stage", body(true));
    setBusy(false);
    if (res.ok) setPreview(res.data);
    else {
      setPreview(null);
      setError(t(lang, moveErrorKey(res.detail)));
    }
  }

  // The preview follows the reasons: without one where it is needed, nothing is asked yet.
  useEffect(() => {
    setPreview(null);
    if (needsReason && !(target.to === "lost" ? lost : override)) return;
    void check();
  }, [lost, override]);

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
          <select value={lost} onChange={(e) => setLost(e.target.value)} style={field} data-testid="bulk-lost-reason">
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
          <select value={override} onChange={(e) => setOverride(e.target.value)} style={field} data-testid="bulk-override">
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
