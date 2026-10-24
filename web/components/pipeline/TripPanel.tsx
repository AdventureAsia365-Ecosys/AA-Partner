"use client";
// web/components/pipeline/TripPanel.tsx — the trip workspace of Slice 1 (AA-936). Moves follow the API's
// allowed_moves (a lost trip needs one of the fixed reasons, R23); the owner assigns anyone, others may take an
// unassigned trip (R8); contact details open per click and the API audits each one (design v3 §4.4).
// The page is a full server render (the board card is a plain link): until React hydrates, a click on a button does
// nothing. So the action buttons stay disabled until then and the root carries `data-hydrated="true"` once handlers
// are attached (AA-936 E2E run 40060691620 clicked "Show email" before hydration and no reveal reached the API).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import {
  codeLabel,
  LOST_REASONS,
  memberLabel,
  partySummary,
  stageLabel,
  waitingLabel,
  WAITING_ON,
  whenLabel,
  type Member,
  type Stage,
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

export function TripPanel({ view, members, role, lang, locale }: { view: TripView; members: Member[]; role: Role; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const { trip } = view;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lostOpen, setLostOpen] = useState(false);
  const [lostReason, setLostReason] = useState<string>("");
  const [note, setNote] = useState("");
  const [revealed, setRevealed] = useState<Record<string, string>>({});
  const hydrated = useHydrated();
  const me = members.find((m) => m.is_me);

  async function run(path: string, method: "POST" | "PATCH", body: unknown) {
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

  async function reveal(travelerId: string, which: "email" | "phone") {
    const res = await bffSend<{ value: string | null }>("POST", `travelers/${travelerId}/reveal`, { field: which });
    if (res.ok) setRevealed((r) => ({ ...r, [`${travelerId}:${which}`]: res.data.value ?? "—" }));
    else setError(t(lang, "trip.error"));
  }

  const owner = members.find((m) => m.member_id === trip.owner_member_id);
  const canTake = !trip.owner_member_id && role !== "owner" && me;
  return (
    <div data-testid="crm-trip" data-card="AA-936" data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 14, maxWidth: 1080, minWidth: 0 }}>
      <a href="/pipeline" style={{ fontSize: 13, color: K.accentDeep }}>
        ← {t(lang, "trip.back")}
      </a>
      <header style={{ display: "grid", gap: 8 }}>
        <h1 style={{ margin: 0, fontFamily: "'Fahkwang', 'Poppins', Georgia, serif", fontSize: 24, fontWeight: 500, color: K.ink, overflowWrap: "anywhere" }} data-testid="trip-title">
          {trip.title}
        </h1>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", fontSize: 13 }}>
          <span data-testid="trip-stage" data-stage={trip.stage} style={{ display: "contents" }}>
            <Badge tone="accent">{stageLabel(lang, trip.stage)}</Badge>
          </span>
          {trip.lost_reason_code && <Badge tone="neutral">{codeLabel(lang, "lost", trip.lost_reason_code)}</Badge>}
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
          {view.allowed_moves
            .filter((s) => s !== "lost")
            .map((s: Stage) => (
              // The kit Button is a vendored CIS copy without a test id: the wrapper carries it (display: contents).
              <span key={s} data-testid={`trip-move-${s}`} style={{ display: "contents" }}>
                <Button size="sm" variant="primary" disabled={busy || !hydrated} onClick={() => void run(`trips/${trip.trip_id}/stage`, "POST", { to: s })}>
                  {trip.stage === "lost" ? t(lang, "trip.reopen") : fmt(t(lang, "trip.moveTo"), { stage: stageLabel(lang, s) })}
                </Button>
              </span>
            ))}
          {view.allowed_moves.includes("lost") && !lostOpen && (
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setLostOpen(true)}>
              {t(lang, "trip.markLost")}
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
                  if (await run(`trips/${trip.trip_id}/stage`, "POST", { to: "lost", lost_reason: lostReason })) setLostOpen(false);
                }}
              >
                {t(lang, "trip.confirmLost")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setLostOpen(false)}>
                {t(lang, "trip.cancel")}
              </Button>
            </span>
          )}
        </div>
        {error && (
          <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
            {error}
          </p>
        )}
      </header>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 14, alignItems: "start" }}>
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
        </Panel>

        <Panel title={t(lang, "trip.travellers")} testId="trip-party">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
            {view.party.map((p) => (
              <li key={p.traveler_id} style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 13.5, color: K.ink, overflowWrap: "anywhere" }}>
                  {p.display_name} <span style={{ color: K.muted, fontSize: 12 }}>· {t(lang, p.role === "lead" ? "trip.lead" : "trip.companion")}</span>
                </span>
                {p.preferred_channel && <span style={{ fontSize: 12, color: K.muted }}>{fmt(t(lang, "trip.prefers"), { channel: codeLabel(lang, "channel", p.preferred_channel) })}</span>}
                <span style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {p.has_email &&
                    (revealed[`${p.traveler_id}:email`] ? (
                      <code data-testid="revealed-email" style={{ fontSize: 12.5, overflowWrap: "anywhere" }}>{revealed[`${p.traveler_id}:email`]}</code>
                    ) : (
                      <span data-testid="show-email" style={{ display: "contents" }}>
                        <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => void reveal(p.traveler_id, "email")}>
                          {t(lang, "trip.showEmail")}
                        </Button>
                      </span>
                    ))}
                  {p.has_phone &&
                    (revealed[`${p.traveler_id}:phone`] ? (
                      <code style={{ fontSize: 12.5 }}>{revealed[`${p.traveler_id}:phone`]}</code>
                    ) : (
                      <span data-testid="show-phone" style={{ display: "contents" }}>
                        <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => void reveal(p.traveler_id, "phone")}>
                          {t(lang, "trip.showPhone")}
                        </Button>
                      </span>
                    ))}
                </span>
              </li>
            ))}
          </ul>
          <p style={{ margin: "10px 0 0", fontSize: 11.5, color: K.muted }}>{t(lang, "trip.revealNote")}</p>
        </Panel>

        <Panel title={t(lang, "trip.tasks")} testId="trip-tasks">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
            {view.tasks.map((task) => (
              <li key={task.task_id} data-testid="trip-task" data-kind={task.kind} data-status={task.status} style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
                <span style={{ fontSize: 13.5, textDecoration: task.status === "open" ? "none" : "line-through", color: task.status === "open" ? K.ink : K.muted }}>
                  {codeLabel(lang, "task", task.kind)}
                  {task.due_at && task.status === "open" ? ` · ${fmt(t(lang, "trip.due"), { when: new Date(task.due_at).toLocaleString(locale, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) })}` : ""}
                </span>
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

        <Panel title={t(lang, "trip.history")} testId="trip-history">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
            {view.history.map((h, i) => (
              <li key={i} style={{ fontSize: 12.5, color: K.body }}>
                {formatDate(h.at, locale)} · {historyLabel(lang, h.reason_code)} → {stageLabel(lang, h.to_stage)}
              </li>
            ))}
          </ul>
        </Panel>
      </div>
    </div>
  );
}

function historyLabel(lang: Lang, reason: string): string {
  if (reason.startsWith("lost_")) return t(lang, "reason.lost");
  return t(lang, `reason.${reason}` as MessageKey) ?? reason;
}
