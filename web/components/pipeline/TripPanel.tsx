"use client";
// web/components/pipeline/TripPanel.tsx — the trip workspace of Slice 1 (AA-936). Moves follow the API's
// allowed_moves (a lost trip needs one of the fixed reasons, R23); the owner assigns anyone, others may take an
// unassigned trip (R8); contact details open per click and the API audits each one (design v3 §4.4).
// The page is a full server render (the board card is a plain link): until React hydrates, a click on a button does
// nothing. So the action buttons stay disabled until then and the root carries `data-hydrated="true"` once handlers
// are attached (AA-936 E2E run 40060691620 clicked "Show email" before hydration and no reveal reached the API).
// AA-937 (CRM-09): a reveal asks why (RevealField, one event with the reason); names link to the client profile;
// companions are added by name only (Fix E5) or taken off the trip; start / end dates (a changed start date moves the
// party's age bands, R32); the trip's documents.
// AA-943 (CRM-15): the stage menu offers every allowed move; a gated move answered 409 opens the gate dialog (go ahead
// with a logged override reason); every move shows the Undo toast (10 s, POST /trips/{id}/stage/undo, which also takes
// back the planning form an auto-send queued); Cancel trip runs the dry run first (owner / agent); the Traveller info
// panel (completeness, planning form, fill in from call); the timeline names override, date-rule and undo reasons.
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { DocumentsPanel } from "@/components/clients/DocumentsPanel";
import { CancelDialog, GateDialog } from "@/components/pipeline/StageDialogs";
import { TravellerInfoPanel } from "@/components/pipeline/TravellerInfoPanel";
import { toast, UndoToast, type ToastState } from "@/components/pipeline/UndoToast";
import { RevealField } from "@/components/clients/RevealField";
import { Panel } from "@/components/PageTitle";
import type { DocumentRow } from "@/lib/clients";
import { bffSend } from "@/lib/bffClient";
import { deleteRefusal } from "@/lib/lifecycle";
import { formatDate, formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import {
  codeLabel,
  gateRefusal,
  LOST_REASONS,
  memberLabel,
  moveErrorKey,
  partySummary,
  stageLabel,
  supplierChecklistLine,
  waitingLabel,
  WAITING_ON,
  whenLabel,
  type GateRefusal,
  type Cancellation,
  type HistoryRow,
  type Member,
  type MoveResult,
  type Stage,
  type Stage2View,
  type TripView,
} from "@/lib/pipeline";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  maxWidth: "100%",
};

export function TripPanel({
  view,
  members,
  role,
  lang,
  locale,
  documents = [],
  stage2,
  clientOrigin = null,
  lostHint = null,
  timeZone = "UTC",
}: {
  view: TripView;
  members: Member[];
  role: Role;
  lang: Lang;
  locale: DateLocale;
  documents?: DocumentRow[];
  /** AA-943: GET /trips/{id}/stage2 (null = it failed; undefined = not asked, the panel is left out). */
  stage2?: Stage2View | null;
  clientOrigin?: string | null;
  /** AA-947: the Lost reason the AA lines suggest (GET /trips/{id}/lines lost_reason_hint); pre-selected, changeable. */
  lostHint?: string | null;
  /** The agency's IANA zone (task due times): the server and the browser print the same time (S262, AA-957). */
  timeZone?: string;
}) {
  const router = useRouter();
  const { trip } = view;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lostOpen, setLostOpen] = useState(false);
  const [lostReason, setLostReason] = useState<string>("");
  const [note, setNote] = useState("");
  const hydrated = useHydrated();
  const [companion, setCompanion] = useState("");
  const [dates, setDates] = useState({ start: trip.start_date ?? "", end: trip.end_date ?? "" });
  const [gate, setGate] = useState<{ to: Stage; refusal: GateRefusal } | null>(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [fillOpen, setFillOpen] = useState(false);
  const [toastState, setToast] = useState<ToastState | null>(null);
  // S262 (AA-957 live): the cancel's answer is shown at once (stage, "Cancellation pending", moves frozen) while the
  // page reloads its server data; the server's view wins as soon as it has the cancellation (one per trip, never
  // removed) or the cancelled stage.
  const [ran, setRan] = useState<{ stage: Stage; cancellation: Cancellation | null } | null>(null);
  const [refreshing, startRefresh] = useTransition();
  const caughtUp = Boolean(view.cancellation) || trip.stage === "cancelled";
  const cancellation = caughtUp || !ran ? (view.cancellation ?? null) : ran.cancellation;
  const stageShown: Stage = caughtUp || !ran ? trip.stage : ran.stage;
  const cancelPending = cancellation?.status === "pending";
  // No move while AA has not confirmed the cancel (the API answers 409 cancellation_pending), nor before the page
  // has the cancel's result.
  const frozen = cancelPending || (ran !== null && !caughtUp);
  const me = members.find((m) => m.is_me);

  async function undoMove(historyId: string) {
    const res = await bffSend<{ dispatch_cancelled: number; stage: Stage }>("POST", `trips/${trip.trip_id}/stage/undo`, { history_id: historyId });
    if (res.ok) {
      const n = res.data.dispatch_cancelled;
      setToast(toast(n > 0 ? fmt(t(lang, "undo.undoneCancelled"), { n }) : t(lang, "pipeline.undone"), { data: { "undone-stage": res.data.stage, "dispatch-cancelled": n } }));
    } else setToast(toast(t(lang, moveErrorKey(res.detail)), { tone: "error" }));
    router.refresh();
  }

  /** One move of the stage menu: a gate refusal opens the gate dialog, every success offers Undo for 10 s. */
  async function move(to: Stage, extra: { lost_reason?: string; override_reason?: string } = {}): Promise<boolean> {
    setBusy(true);
    setError(null);
    const res = await bffSend<MoveResult>("POST", `trips/${trip.trip_id}/stage`, { to, ...extra });
    setBusy(false);
    if (!res.ok) {
      const refusal = gateRefusal(res.body, trip.stage, to);
      if (refusal && !extra.override_reason) setGate({ to, refusal });
      else setError(t(lang, moveErrorKey(res.detail)));
      return false;
    }
    const data = res.data;
    const stage = stageLabel(lang, data.stage);
    const message = data.stage2_sent
      ? fmt(t(lang, "undo.movedSent"), { stage })
      : data.stage2_skipped
        ? fmt(t(lang, "undo.notSent"), { stage, why: t(lang, (`skip.${data.stage2_skipped}` as MessageKey)) ?? t(lang, "skip.other") })
        : fmt(t(lang, "pipeline.moved"), { stage });
    const historyId = data.undo?.history_id;
    setToast(toast(message, { undo: historyId ? () => undoMove(historyId) : undefined, data: { stage: data.stage, "stage2-sent": data.stage2_sent ? "true" : "false" } }));
    router.refresh();
    return true;
  }

  async function run(path: string, method: "POST" | "PATCH" | "DELETE", body?: unknown) {
    setBusy(true);
    setError(null);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setError(t(lang, "trip.error"));
      return false;
    }
    router.refresh();
    return true;
  }

  const owner = members.find((m) => m.member_id === trip.owner_member_id);
  const canTake = !trip.owner_member_id && role !== "owner" && me;
  return (
    <div data-testid="crm-trip" data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 14, maxWidth: 1080, minWidth: 0 }}>
      <a href="/pipeline" style={{ fontSize: 13, color: K.accentDeep }}>
        ← {t(lang, "trip.back")}
      </a>
      <header style={{ display: "grid", gap: 8 }}>
        <h1 style={{ margin: 0, fontFamily: "'Fahkwang', 'Poppins', Georgia, serif", fontSize: 24, fontWeight: 500, color: K.ink, overflowWrap: "anywhere" }} data-testid="trip-title">
          {trip.title}
        </h1>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", fontSize: 13 }}>
          <span data-testid="trip-stage" data-stage={stageShown} style={{ display: "contents" }}>
            <Badge tone="accent">{stageLabel(lang, stageShown)}</Badge>
          </span>
          {trip.lost_reason_code && <Badge tone="neutral">{codeLabel(lang, "lost", trip.lost_reason_code)}</Badge>}
          {trip.cancel_reason_code && <Badge tone="danger">{codeLabel(lang, "cancel", trip.cancel_reason_code)}</Badge>}
          {cancelPending && (
            <span data-testid="trip-cancellation-pending" title={t(lang, "trip.cancellationPendingHint")} style={{ display: "contents" }}>
              <Badge tone="danger">{t(lang, "trip.cancellationPending")}</Badge>
            </span>
          )}
          {trip.quiet && typeof trip.quiet_days === "number" && (
            <span data-testid="trip-quiet" style={{ display: "contents" }}>
              <Badge tone="warning">{fmt(t(lang, "trip.quiet"), { n: trip.quiet_days })}</Badge>
            </span>
          )}
          <label style={{ display: "flex", gap: 6, alignItems: "center", color: K.muted }}>
            {t(lang, "trip.waitingOn")}
            <select
              data-testid="trip-waiting"
              value={trip.waiting_on}
              disabled={busy}
              onChange={(e) => void run(`trips/${trip.trip_id}`, "PATCH", { waiting_on: e.target.value })}
              style={field}
            >
              {WAITING_ON.map((w) => (
                <option key={w} value={w}>
                  {waitingLabel(lang, w)}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "flex", gap: 6, alignItems: "center", color: K.muted }}>
            {t(lang, "trip.owner")}
            {role === "owner" ? (
              <select
                data-testid="trip-owner"
                value={trip.owner_member_id ?? ""}
                disabled={busy}
                onChange={(e) => void run(`trips/${trip.trip_id}/owner`, "POST", { member_id: e.target.value || null })}
                style={field}
              >
                <option value="">{t(lang, "pipeline.unassigned")}</option>
                {members
                  .filter((m) => m.status === "active")
                  .map((m) => (
                    <option key={m.member_id} value={m.member_id}>
                      {memberLabel(lang, m)}
                    </option>
                  ))}
              </select>
            ) : (
              <span style={{ color: K.ink }}>{owner ? memberLabel(lang, owner) : t(lang, "pipeline.unassigned")}</span>
            )}
          </label>
          {canTake && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => void run(`trips/${trip.trip_id}/owner`, "POST", { member_id: me.member_id })}>
              {t(lang, "trip.take")}
            </Button>
          )}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }} data-testid="trip-moves">
          {(frozen ? [] : view.allowed_moves)
            .filter((s) => s !== "lost")
            .map((s: Stage) => (
              // The kit Button is a vendored CIS copy without a test id: the wrapper carries it (display: contents).
              <span key={s} data-testid={`trip-move-${s}`} style={{ display: "contents" }}>
                <Button size="sm" variant="primary" disabled={busy || !hydrated} onClick={() => void move(s)}>
                  {trip.stage === "lost" ? t(lang, "trip.reopen") : fmt(t(lang, "trip.moveTo"), { stage: stageLabel(lang, s) })}
                </Button>
              </span>
            ))}
          {!frozen && view.allowed_moves.includes("lost") && !lostOpen && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                if (lostHint && LOST_REASONS.includes(lostHint as (typeof LOST_REASONS)[number])) setLostReason(lostHint);
                setLostOpen(true);
              }}
            >
              {t(lang, "trip.markLost")}
            </Button>
          )}
          {view.can_cancel && role !== "assistant" && stageShown !== "cancelled" && (
            // While AA has not confirmed it, the button only opens the cancellation's state ("Ask AA again").
            <span data-testid="trip-cancel" data-pending={cancelPending ? "true" : undefined} style={{ display: "contents" }}>
              <Button size="sm" variant={cancelPending ? "secondary" : "danger"} disabled={busy || !hydrated || refreshing} onClick={() => setCancelOpen(true)}>
                {t(lang, cancelPending ? "cancelTrip.pendingOpen" : "cancelTrip.open")}
              </Button>
            </span>
          )}
          {role !== "assistant" && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={async () => {
                // AA-942: to the trash (Settings > Data and privacy restores it); a trip with money, a signature, a
                // commission or a held / confirmed AA line is refused and the agent is told to cancel it instead.
                if (!window.confirm(t(lang, "trip.deleteConfirm"))) return;
                setBusy(true);
                setError(null);
                const res = await bffSend("DELETE", `trips/${trip.trip_id}`);
                setBusy(false);
                if (res.ok) router.push("/pipeline");
                else setError(t(lang, deleteRefusal(res.detail)));
              }}
            >
              {t(lang, "trip.delete")}
            </Button>
          )}
          {lostOpen && (
            <span style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <select aria-label={t(lang, "trip.lostReason")} value={lostReason} onChange={(e) => setLostReason(e.target.value)} style={field} data-testid="trip-lost-reason">
                <option value="">{t(lang, "trip.lostReason")}</option>
                {LOST_REASONS.map((r) => (
                  <option key={r} value={r}>
                    {codeLabel(lang, "lost", r)}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                variant="danger"
                disabled={busy || !lostReason}
                onClick={async () => {
                  if (await move("lost", { lost_reason: lostReason })) setLostOpen(false);
                }}
              >
                {t(lang, "trip.confirmLost")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setLostOpen(false)}>
                {t(lang, "trip.cancel")}
              </Button>
              {lostHint && lostReason === lostHint && (
                <span style={{ fontSize: 12, color: K.muted }} data-testid="trip-lost-hint">
                  {t(lang, "aa.lostHint")}
                </span>
              )}
            </span>
          )}
        </div>
        {error && (
          <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
            {error}
          </p>
        )}
      </header>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(300px, 100%), 1fr))", gap: 14, alignItems: "start" }}>
        {stage2 !== undefined && (
          <TravellerInfoPanel
            view={view}
            stage2={stage2}
            clientOrigin={clientOrigin}
            lang={lang}
            locale={locale}
            fillOpen={fillOpen}
            setFillOpen={setFillOpen}
            notify={setToast}
          />
        )}
        <Panel title={t(lang, "trip.details")} testId="trip-details">
          <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "6px 14px", margin: 0, fontSize: 13.5 }}>
            <dt style={{ color: K.muted }}>{t(lang, "trip.destinations")}</dt>
            <dd style={{ margin: 0 }}>{trip.destinations.map((d) => codeLabel(lang, "dest", d)).join(", ") || "—"}</dd>
            <dt style={{ color: K.muted }}>{t(lang, "trip.when")}</dt>
            <dd style={{ margin: 0 }}>
              {[whenLabel(trip, locale), trip.nights ? fmt(t(lang, "trip.nights"), { n: trip.nights }) : null, trip.date_flex_days ? fmt(t(lang, "trip.flex"), { n: trip.date_flex_days }) : null]
                .filter(Boolean)
                .join(" · ") || "—"}
            </dd>
            <dt style={{ color: K.muted }}>{t(lang, "trip.party")}</dt>
            <dd style={{ margin: 0 }}>
              {partySummary(lang, trip.party_adults, trip.party_child_ages)}
              {trip.party_type ? ` · ${codeLabel(lang, "party", trip.party_type)}` : ""}
            </dd>
            <dt style={{ color: K.muted }}>{t(lang, "trip.budget")}</dt>
            <dd style={{ margin: 0 }}>{codeLabel(lang, "budget", trip.budget_band)}</dd>
            <dt style={{ color: K.muted }}>{t(lang, "trip.style")}</dt>
            <dd style={{ margin: 0 }}>{codeLabel(lang, "style", trip.trip_style)}</dd>
            <dt style={{ color: K.muted }}>{t(lang, "trip.source")}</dt>
            <dd style={{ margin: 0 }}>{trip.lead_source ?? "—"}</dd>
          </dl>
          <form
            data-testid="trip-dates"
            onSubmit={(e) => {
              e.preventDefault();
              void run(`trips/${trip.trip_id}`, "PATCH", { start_date: dates.start || null, end_date: dates.end || null });
            }}
            style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12, alignItems: "end" }}
          >
            <label style={{ display: "grid", gap: 3, fontSize: 12, color: K.muted }}>
              {t(lang, "trip.startDate")}
              <input type="date" value={dates.start} onChange={(e) => setDates((d) => ({ ...d, start: e.target.value }))} style={field} />
            </label>
            <label style={{ display: "grid", gap: 3, fontSize: 12, color: K.muted }}>
              {t(lang, "trip.endDate")}
              <input type="date" value={dates.end} onChange={(e) => setDates((d) => ({ ...d, end: e.target.value }))} style={field} />
            </label>
            <Button size="sm" type="submit" variant="secondary" disabled={busy || !hydrated || (dates.start === (trip.start_date ?? "") && dates.end === (trip.end_date ?? ""))}>
              {t(lang, "trip.saveDates")}
            </Button>
          </form>
        </Panel>

        <Panel title={t(lang, "trip.travellers")} testId="trip-party">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {view.party.map((p) => (
              <li key={p.traveler_id} style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 13.5, color: K.ink, overflowWrap: "anywhere" }}>
                  <a href={`/clients/${p.traveler_id}`} title={t(lang, "trip.openClient")} style={{ color: K.ink }}>
                    {p.display_name}
                  </a>{" "}
                  <span style={{ color: K.muted, fontSize: 12 }}>· {t(lang, p.role === "lead" ? "trip.lead" : "trip.companion")}</span>
                </span>
                {p.preferred_channel && <span style={{ fontSize: 12, color: K.muted }}>{fmt(t(lang, "trip.prefers"), { channel: codeLabel(lang, "channel", p.preferred_channel) })}</span>}
                <span style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                  {p.has_email && <RevealField travelerId={p.traveler_id} field="email" filled lang={lang} label={t(lang, "field.email")} />}
                  {p.has_phone && <RevealField travelerId={p.traveler_id} field="phone" filled lang={lang} label={t(lang, "field.phone")} />}
                </span>
                {p.role === "companion" && (
                  <span>
                    <Button size="sm" variant="ghost" disabled={busy || !hydrated} onClick={() => void run(`trips/${trip.trip_id}/travelers/${p.traveler_id}`, "DELETE")}>
                      {t(lang, "trip.removeTraveller")}
                    </Button>
                  </span>
                )}
              </li>
            ))}
          </ul>
          <form
            data-testid="trip-add-companion"
            onSubmit={async (e) => {
              e.preventDefault();
              if (companion.trim() && (await run(`trips/${trip.trip_id}/travelers`, "POST", { new: { display_name: companion } }))) setCompanion("");
            }}
            style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10, alignItems: "center" }}
          >
            <input aria-label={t(lang, "trip.companionName")} placeholder={t(lang, "trip.companionName")} value={companion} onChange={(e) => setCompanion(e.target.value)} maxLength={120} style={{ ...field, flex: "1 1 160px" }} />
            <Button size="sm" type="submit" variant="secondary" disabled={busy || !hydrated || !companion.trim()}>
              {t(lang, "trip.add")}
            </Button>
            <span style={{ width: "100%", fontSize: 11.5, color: K.muted }}>{t(lang, "trip.companionHelp")}</span>
          </form>
          <p style={{ margin: "10px 0 0", fontSize: 11.5, color: K.muted }}>{t(lang, "trip.revealNote")}</p>
        </Panel>

        <Panel title={t(lang, "trip.tasks")} testId="trip-tasks">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
            {view.tasks.map((task) => (
              <li key={task.task_id} data-testid="trip-task" data-kind={task.kind} data-status={task.status} style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
                <span style={{ fontSize: 13.5, textDecoration: task.status === "open" ? "none" : "line-through", color: task.status === "open" ? K.ink : K.muted }}>
                  {task.priority === "urgent" && task.status === "open" && <Badge tone="danger">{t(lang, "trip.urgent")}</Badge>} {codeLabel(lang, "task", task.kind)}
                  {task.due_at && task.status === "open" ? ` · ${fmt(t(lang, "trip.due"), { when: formatInstant(task.due_at, locale, timeZone) })}` : ""}
                </span>
                {task.checklist && task.checklist.length > 0 && (
                  <ul data-testid="trip-task-checklist" style={{ width: "100%", margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.ink, display: "grid", gap: 2 }}>
                    {task.checklist.map((item) => (
                      <li key={item.agency_supplier_id}>{supplierChecklistLine(item)}</li>
                    ))}
                  </ul>
                )}
                <Button
                  size="sm"
                  variant={task.status === "open" ? "secondary" : "ghost"}
                  disabled={busy}
                  onClick={() => void run(`tasks/${task.task_id}/status`, "POST", { status: task.status === "open" ? "done" : "open" })}
                >
                  {task.status === "open" ? t(lang, "trip.done") : t(lang, "trip.reopenTask")}
                </Button>
              </li>
            ))}
          </ul>
        </Panel>

        <Panel title={t(lang, "trip.notes")} testId="trip-notes">
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (note.trim() && (await run(`trips/${trip.trip_id}/notes`, "POST", { body: note }))) setNote("");
            }}
            style={{ display: "grid", gap: 6, marginBottom: 10 }}
          >
            <textarea aria-label={t(lang, "trip.addNote")} placeholder={t(lang, "trip.addNote")} value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={4000} style={{ ...field, width: "100%", boxSizing: "border-box" }} />
            <div>
              <Button size="sm" type="submit" variant="secondary" disabled={busy || !note.trim()}>
                {t(lang, "trip.save")}
              </Button>
            </div>
          </form>
          {view.notes.length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "trip.noNotes")}</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
              {view.notes.map((n) => (
                <li key={n.note_id} style={{ display: "grid", gap: 2 }}>
                  <span style={{ fontSize: 11.5, color: K.muted }}>
                    {codeLabel(lang, "note", n.kind)} · {formatDate(n.created_at, locale)}
                  </span>
                  <span style={{ fontSize: 13.5, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{n.body}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title={t(lang, "trip.answers")} testId="trip-answers">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {view.submissions.map((s) => (
              <li key={s.submission_id} style={{ fontSize: 13, color: K.body, display: "grid", gap: 2 }}>
                <span style={{ fontSize: 11.5, color: K.muted }}>
                  {formatDate(s.created_at, locale)}
                  {s.outcome === "attached" ? ` · ${t(lang, "trip.attached")}` : ""}
                </span>
                {s.answers && (
                  <span>
                    {[
                      Array.isArray(s.answers.destinations) ? (s.answers.destinations as string[]).map((d) => codeLabel(lang, "dest", d)).join(", ") : null,
                      typeof s.answers.nights === "number" ? fmt(t(lang, "trip.nights"), { n: s.answers.nights }) : null,
                      typeof s.answers.budget_band === "string" ? codeLabel(lang, "budget", s.answers.budget_band) : null,
                      typeof s.answers.heard_from === "string" ? codeLabel(lang, "heard", s.answers.heard_from) : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Panel>

        <DocumentsPanel documents={documents} tripId={trip.trip_id} role={role} lang={lang} locale={locale} />

        <Panel title={t(lang, "trip.history")} testId="trip-history">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
            {view.history.map((h, i) => (
              <li key={h.history_id ?? i} data-reason={h.reason_code} style={{ fontSize: 12.5, color: K.body }}>
                {formatDate(h.at, locale)} · {historyLabel(lang, h)} → {stageLabel(lang, h.to_stage)}
                {h.override_reason_code ? ` · ${fmt(t(lang, "history.override"), { reason: codeLabel(lang, "override", h.override_reason_code) })}` : ""}
              </li>
            ))}
          </ul>
        </Panel>
      </div>
      {gate && (
        <GateDialog
          refusal={gate.refusal}
          lang={lang}
          onClose={() => setGate(null)}
          onFill={
            stage2
              ? () => {
                  setGate(null);
                  setFillOpen(true);
                }
              : undefined
          }
          onConfirm={async (reason) => {
            // Closed either way: a refusal of the override is shown on the page.
            await move(gate.to, { override_reason: reason });
            setGate(null);
          }}
        />
      )}
      {cancelOpen && (
        <CancelDialog
          tripId={trip.trip_id}
          lang={lang}
          locale={locale}
          onClose={() => setCancelOpen(false)}
          onDone={(result) => {
            setCancelOpen(false);
            // AA-957: with AA bookings the trip waits for AA ("Cancellation pending", never cancelled early).
            const pending = result.cancellation?.status === "pending";
            const key = pending ? (result.requeued !== undefined ? "cancelTrip.retried" : "cancelTrip.pendingDone") : "cancelTrip.done";
            setRan({ stage: result.stage ?? trip.stage, cancellation: result.cancellation ?? null });
            setToast(toast(t(lang, key)));
            startRefresh(() => router.refresh());
          }}
        />
      )}
      <UndoToast state={toastState} lang={lang} onClose={() => setToast(null)} />
    </div>
  );
}

export function historyLabel(lang: Lang, h: Pick<HistoryRow, "reason_code">): string {
  const reason = h.reason_code;
  if (reason.startsWith("lost_")) return t(lang, "reason.lost");
  if (reason.startsWith("cancel_")) return `${t(lang, "reason.cancel")}: ${codeLabel(lang, "cancel", reason.slice("cancel_".length))}`;
  return t(lang, `reason.${reason}` as MessageKey) ?? reason;
}
