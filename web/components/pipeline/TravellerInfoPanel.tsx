"use client";
// web/components/pipeline/TravellerInfoPanel.tsx — "Traveller info" on the trip page (AA-943, CRM-15; design v3 §2.7
// F2, spec §5): how complete each intake stage is and what is missing.
//
//   - Basics (Stage 1, from the inquiry);
//   - Planning info (Stage 2): the percentage the Proposal gate reads (80 %), the missing answers, the planning-form
//     link's state, [Send planning form] (a signed link through the dispatcher; held until a mail sender exists, so the
//     link is shown once here to copy), [Fill in from call] (the same paged form, the agent typing), and the answers
//     the client gave so far (bands and codes only, F2);
//   - Booking details (Stage 3): before the trip is booked it opens later; once booked the travellers send it from
//     their own page (the Proposal card's Stage 3 progress); on a cancelled trip it is not needed (AA-957 round 3).
//
// The token is never stored: "Copy link" works from the send answer of this page view only (a new send re-issues it).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { PlanningForm, type SubmitResult } from "@/components/intake/PlanningForm";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { dictionary, fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import { bookingDetailsKey, STAGE2_GATE, type SendResult, type Stage2View, type TripView } from "@/lib/pipeline";
import { OPTION_PREFIX, type Answers } from "@/lib/planningForm";
import { Dialog, missingLabel } from "./StageDialogs";
import { toast, type ToastState } from "./UndoToast";

export function percentTone(n: number | undefined): "success" | "warning" | "neutral" {
  if (n === undefined) return "neutral";
  return n >= STAGE2_GATE ? "success" : "warning";
}

/** One answer of the F2 profile worded for the panel ("Comfort", "Culture, Food", "Yes"). */
export function answerText(lang: Lang, name: string, value: unknown): string {
  const prefix = OPTION_PREFIX[name];
  const word = (code: string) => (prefix ? (t(lang, `${prefix}.${code}` as MessageKey) ?? code) : code);
  if (typeof value === "boolean") return t(lang, value ? "common.yes" : "common.no");
  if (Array.isArray(value)) return value.map((v) => word(String(v))).join(", ");
  return word(String(value));
}

export function TravellerInfoPanel({
  view,
  stage2,
  clientOrigin,
  lang,
  locale,
  fillOpen,
  setFillOpen,
  notify,
  closed: tripClosed = false,
}: {
  view: TripView;
  stage2: Stage2View | null;
  /** https://<client host>, where the planning form link opens; null when no client host is configured. */
  clientOrigin: string | null;
  lang: Lang;
  locale: DateLocale;
  fillOpen: boolean;
  setFillOpen: (open: boolean) => void;
  notify: (state: ToastState) => void;
  /** S262 round 3: the trip is cancelled or being cancelled (the page's closed state). */
  closed?: boolean;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const { trip } = view;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<SendResult | null>(null);
  const [copied, setCopied] = useState(false);
  const done = stage2?.completeness;
  const s1 = done?.s1 ?? trip.intake_completeness?.s1;
  const s2 = done?.s2 ?? trip.intake_completeness?.s2;
  const s3 = done?.s3 ?? trip.intake_completeness?.s3;
  const link = view.stage2_link ?? null;
  const live = link && (link.status === "sent" || link.status === "opened");
  const closed = tripClosed || trip.stage === "completed" || trip.stage === "lost" || trip.stage === "cancelled";
  // Without a configured client host (local runs) the path alone is shown.
  const linkUrl = sent ? `${clientOrigin ?? ""}${sent.link_path}` : null;

  async function send() {
    setBusy(true);
    setError(null);
    const res = await bffSend<SendResult>("POST", `trips/${trip.trip_id}/stage2/send`);
    setBusy(false);
    if (!res.ok) {
      setError(res.detail === "no_lead_traveller" ? t(lang, "skip.no_lead_traveller") : t(lang, "trip.error"));
      return;
    }
    setSent(res.data);
    notify(toast(t(lang, "info.queued")));
    router.refresh();
  }

  async function fill(body: Answers): Promise<SubmitResult> {
    const res = await bffSend<{ completeness?: { s2: number } | null }>("PUT", `trips/${trip.trip_id}/stage2`, body);
    return res.ok ? { ok: true, data: res.data } : { ok: false, status: res.status, detail: res.detail, body: res.body };
  }

  const answers = Object.entries(stage2?.answers ?? {});
  return (
    <Panel title={t(lang, "info.title")} testId="traveller-info">
      <div style={{ display: "grid", gap: 12, fontSize: 13.5 }}>
        <Row label={t(lang, "info.basics")} percent={s1} testId="info-s1">
          {done && done.missing_s1.length > 0 && <Missing text={fmt(t(lang, "info.missing"), { list: missingLabel(lang, done.missing_s1, "s1") })} />}
        </Row>
        <Row label={t(lang, "info.planning")} percent={s2} testId="info-s2">
          {done && done.missing_s2.length > 0 && (
            <Missing text={fmt(t(lang, "info.missing"), { list: missingLabel(lang, done.missing_s2) })} testId="info-s2-missing" />
          )}
          {(s2 ?? 0) < STAGE2_GATE && (trip.stage === "inquiry" || trip.stage === "qualifying") && <Missing text={t(lang, "info.gateNote")} />}
          {link && (
            <span style={{ fontSize: 12.5, color: K.muted }} data-testid="info-link" data-status={link.status}>
              {linkStatus(lang, locale, link)}
            </span>
          )}
          {!closed && (
            <span style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              <span data-testid="info-send" style={{ display: "contents" }}>
                <Button size="sm" variant="secondary" disabled={busy || !hydrated} onClick={() => void send()}>
                  {t(lang, live ? "info.resend" : "info.send")}
                </Button>
              </span>
              {stage2 && (
                <span data-testid="info-fill" style={{ display: "contents" }}>
                  <Button size="sm" variant="ghost" disabled={busy || !hydrated} onClick={() => setFillOpen(true)}>
                    {t(lang, "info.fill")}
                  </Button>
                </span>
              )}
            </span>
          )}
          {linkUrl && (
            <label style={{ display: "grid", gap: 4, fontSize: 12, color: K.muted }}>
              {t(lang, "info.linkLabel")}
              <span style={{ display: "flex", gap: 6 }}>
                <input readOnly value={linkUrl} data-testid="info-link-url" style={{ font: "inherit", fontSize: 12.5, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line2}`, background: K.bg, color: K.ink, minWidth: 0, flex: 1 }} />
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(linkUrl);
                      setCopied(true);
                      setTimeout(() => setCopied(false), 2000);
                    } catch {
                      setCopied(false);
                    }
                  }}
                >
                  {t(lang, copied ? "info.copied" : "info.copy")}
                </Button>
              </span>
              <span>{t(lang, "info.linkOnce")}</span>
            </label>
          )}
          {error && (
            <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
              {error}
            </p>
          )}
          {stage2 === null && <Missing text={t(lang, "info.unavailable")} />}
          {answers.length > 0 ? (
            <details data-testid="info-answers">
              <summary style={{ cursor: "pointer", fontSize: 12.5, color: K.accentDeep }}>{t(lang, "info.answers")}</summary>
              <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 12px", margin: "8px 0 0", fontSize: 12.5 }}>
                {answers.map(([name, value]) => (
                  <Pair key={name} name={name} label={t(lang, `plan.field.${name}` as MessageKey) ?? name} value={answerText(lang, name, value)} />
                ))}
              </dl>
            </details>
          ) : (
            stage2 && <span style={{ fontSize: 12.5, color: K.muted }}>{t(lang, "info.noAnswers")}</span>
          )}
        </Row>
        <Row label={t(lang, "info.booking")} percent={tripClosed || trip.stage === "cancelled" ? undefined : s3} testId="info-s3">
          <span style={{ fontSize: 12.5, color: K.muted }} data-testid="info-s3-note">
            {t(lang, bookingDetailsKey(trip.stage, tripClosed))}
          </span>
        </Row>
      </div>
      {fillOpen && stage2 && (
        <Dialog title={t(lang, "info.fillTitle")} onClose={() => setFillOpen(false)} testId="fill-dialog">
          <PlanningForm
            pages={stage2.pages}
            tripStyle={trip.trip_style}
            initial={stage2.answers}
            agency=""
            mode="agent"
            dict={dictionary(lang)}
            onSubmit={fill}
            onDone={(data) => {
              setFillOpen(false);
              const n = (data as { completeness?: { s2?: number } | null } | undefined)?.completeness?.s2;
              notify(toast(fmt(t(lang, "info.saved"), { n: n ?? "—" })));
              router.refresh();
            }}
          />
        </Dialog>
      )}
    </Panel>
  );
}

function linkStatus(lang: Lang, locale: DateLocale, link: NonNullable<TripView["stage2_link"]>): string {
  const until = fmt(t(lang, "info.link.until"), { date: formatDate(link.expires_at, locale) });
  switch (link.status) {
    case "submitted":
      return fmt(t(lang, "info.link.submitted"), { date: formatDate(link.submitted_at, locale) });
    case "opened":
      return `${fmt(t(lang, "info.link.opened"), { date: formatDate(link.opened_at, locale) })} · ${until}`;
    case "sent":
      return `${fmt(t(lang, "info.link.sent"), { date: formatDate(link.sent_at, locale) })} · ${until}`;
    default:
      return t(lang, `info.link.${link.status}` as MessageKey);
  }
}

function Row({ label, percent, testId, children }: { label: string; percent: number | undefined; testId: string; children?: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gap: 6 }} data-testid={testId} data-percent={percent ?? ""}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <strong style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{label}</strong>
        <Badge tone={percentTone(percent)} dot={false}>
          {percent === undefined ? "—" : `${percent} %`}
        </Badge>
      </div>
      <div aria-hidden="true" style={{ height: 4, background: K.line, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${Math.max(0, Math.min(100, percent ?? 0))}%`, height: "100%", background: (percent ?? 0) >= STAGE2_GATE ? K.success : K.accent }} />
      </div>
      {children}
    </div>
  );
}

function Missing({ text, testId }: { text: string; testId?: string }) {
  return (
    <span style={{ fontSize: 12.5, color: K.muted }} data-testid={testId}>
      {text}
    </span>
  );
}

function Pair({ name, label, value }: { name: string; label: string; value: string }) {
  return (
    <>
      <dt style={{ color: K.muted }} data-field={name}>
        {label}
      </dt>
      <dd style={{ margin: 0, color: K.body, overflowWrap: "anywhere" }}>{value}</dd>
    </>
  );
}
