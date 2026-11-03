"use client";
// web/components/pipeline/AiDraftPanel.tsx — "AI draft" on the trip workspace (AA-1027 slice F3, gap CRM-4; design v3
// M14, R39 "AI drafts, humans send", A-21). A card with the Draft action; the action opens the kit Drawer: the kind
// (reply / follow-up), the client's message, an optional instruction, then the draft with the kit AiNotice (staff
// variant, AA-1033) under it, an optional feedback code and Copy and accept / Discard. Accepting sends nothing: the
// draft is copied for the member to paste into a message, check and send. The action is disabled, with the reason,
// when the agency's plan has no AI assistant (the API answers 403 feature_not_allowed) or AI is switched off.
// Calls go through the BFF allow-list (lib/bff.ts): POST trips/{id}/ai/draft, POST ai/drafts/{id}/accept|discard.
import Link from "next/link";
import { useState } from "react";
import { AiNotice, Badge, Button, Drawer, Field, Input, K, Select, SegmentedControl, Textarea, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { testModeNote, type AiSettings } from "@/lib/ai";
import {
  DRAFT_KINDS,
  FEEDBACK_CODES,
  MAX_INSTRUCTION,
  MAX_TEXT,
  draftAvailability,
  draftErrorKey,
  draftInputError,
  fallbackKey,
  unavailableKey,
  type DraftDecision,
  type DraftKind,
  type DraftResult,
  type FeedbackCode,
} from "@/lib/aiDraft";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";

type Done = { decision: "accepted" | "discarded"; copied: boolean };

const note: React.CSSProperties = { margin: 0, fontSize: 13, color: K.muted, overflowWrap: "anywhere" };

export function AiDraftPanel({
  tripId,
  settings,
  isOwner,
  lang,
}: {
  tripId: string;
  /** GET settings/ai, or null when it could not be read. */
  settings: AiSettings | null;
  isOwner: boolean;
  lang: Lang;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const state = draftAvailability(settings);
  const testNote = testModeNote(settings);
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<DraftKind>("reply");
  const [text, setText] = useState("");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftResult | null>(null);
  const [feedback, setFeedback] = useState<FeedbackCode | "">("");
  const [done, setDone] = useState<Done | null>(null);

  function reset() {
    setDraft(null);
    setFeedback("");
    setDone(null);
    setError(null);
  }

  async function write(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const invalid = draftInputError(text, instruction);
    if (invalid) {
      setError(tr(invalid));
      return;
    }
    setBusy(true);
    reset();
    const body = { kind, text: text.trim(), ...(instruction.trim() ? { instruction: instruction.trim() } : {}) };
    const res = await bffSend<DraftResult>("POST", `trips/${tripId}/ai/draft`, body, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(draftErrorKey(res.detail)));
      return;
    }
    setDraft(res.data);
  }

  async function decide(action: "accept" | "discard") {
    if (!draft?.ai_draft_id) return;
    let copied = false;
    if (action === "accept" && draft.text) {
      try {
        await navigator.clipboard.writeText(draft.text);
        copied = true;
      } catch {
        copied = false; // the browser refused the clipboard: the member selects the text instead
      }
    }
    setBusy(true);
    setError(null);
    const res = await bffSend<DraftDecision>(
      "POST",
      `ai/drafts/${draft.ai_draft_id}/${action}`,
      feedback ? { feedback_code: feedback } : {},
      { renew: true },
    );
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(draftErrorKey(res.detail)));
      return;
    }
    setDone({ decision: res.data.decision, copied });
  }

  const usable = draft && draft.outcome === "ok" && draft.text != null;

  return (
    <Panel title={tr("aiDraft.title")} testId="ai-draft">
      <div
        data-hydrated={hydrated ? "true" : undefined}
        data-state={state}
        style={{ display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", minWidth: 0 }}
      >
        <div style={{ display: "grid", gap: 4, flex: "1 1 260px", minWidth: 0 }}>
          <p style={note}>{tr("aiDraft.intro")}</p>
          {state !== "ready" && (
            <p data-testid="ai-draft-unavailable" style={{ ...note, color: K.body }}>
              {tr(unavailableKey(state, isOwner))}
              {state === "off" && isOwner && (
                <>
                  {" "}
                  <Link href="/settings/ai" style={{ color: K.accent }}>
                    {tr("aiDraft.settingsLink")}
                  </Link>
                </>
              )}
            </p>
          )}
        </div>
        <Button
          variant="secondary"
          size="sm"
          testId="ai-draft-open"
          disabled={state !== "ready"}
          onClick={() => {
            reset();
            setOpen(true);
          }}
        >
          {tr("aiDraft.open")}
        </Button>
      </div>
      <Drawer open={open} onClose={() => setOpen(false)} title={tr("aiDraft.title")} width={560}>
        <div data-testid="ai-draft-drawer" style={{ display: "grid", gap: 14, minWidth: 0 }}>
          {testNote && (
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }} data-testid="ai-draft-test-mode">
              <Badge tone="info">{tr("ai.testMode")}</Badge>
              <span style={{ fontSize: 12.5, color: K.muted }}>
                {tr(testNote === "free" ? "aiDraft.testMode" : "aiDraft.testModeScrub")}
              </span>
            </div>
          )}
          <form onSubmit={write} style={{ display: "grid", gap: 12, minWidth: 0 }} data-testid="ai-draft-form">
            <SegmentedControl<DraftKind>
              label={tr("aiDraft.kind")}
              value={kind}
              onChange={setKind}
              options={DRAFT_KINDS.map((k) => ({ value: k, label: tr(`aiDraft.kind.${k}`) }))}
            />
            <Field label={tr("aiDraft.text")} hint={tr("aiDraft.textHelp")} required>
              <Textarea rows={6} maxLength={MAX_TEXT} value={text} onChange={(e) => setText(e.target.value)} />
            </Field>
            <Field label={tr("aiDraft.instruction")} hint={tr("aiDraft.instructionHelp")}>
              <Input maxLength={MAX_INSTRUCTION} value={instruction} onChange={(e) => setInstruction(e.target.value)} />
            </Field>
            <div>
              <Button type="submit" variant="primary" disabled={busy} testId="ai-draft-write">
                {busy && !draft ? tr("aiDraft.writing") : draft ? tr("aiDraft.another") : tr("aiDraft.write")}
              </Button>
            </div>
          </form>
          {error && (
            <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger, overflowWrap: "anywhere" }}>
              {error}
            </p>
          )}
          {draft && !usable && (
            <p role="status" data-testid="ai-draft-fallback" style={{ margin: 0, fontSize: 13, color: K.amber }}>
              {tr(fallbackKey(draft))}
            </p>
          )}
          {usable && (
            <section data-testid="ai-draft-result" style={{ display: "grid", gap: 8, minWidth: 0 }}>
              <h3 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: K.ink }}>{tr("aiDraft.result")}</h3>
              <div
                data-testid="ai-draft-text"
                style={{
                  whiteSpace: "pre-wrap",
                  overflowWrap: "anywhere",
                  fontSize: 14,
                  lineHeight: 1.55,
                  color: K.body,
                  background: K.bg,
                  border: `1px solid ${K.line}`,
                  borderRadius: 8,
                  padding: "10px 12px",
                }}
              >
                {draft.text}
              </div>
              <AiNotice lang={lang} />
              {done ? (
                <p role="status" data-testid="ai-draft-done" data-decision={done.decision} style={{ ...note, color: K.body }}>
                  {tr(done.decision === "discarded" ? "aiDraft.discarded" : done.copied ? "aiDraft.accepted" : "aiDraft.acceptedNoCopy")}
                </p>
              ) : (
                <>
                  <Field label={tr("aiDraft.feedback")}>
                    <Select
                      value={feedback}
                      onChange={(e) => setFeedback(e.target.value as FeedbackCode | "")}
                      options={[
                        { value: "", label: tr("aiDraft.feedback.none") },
                        ...FEEDBACK_CODES.map((code) => ({ value: code, label: tr(`aiDraft.feedback.${code}`) })),
                      ]}
                    />
                  </Field>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <Button variant="primary" disabled={busy} onClick={() => decide("accept")} testId="ai-draft-accept">
                      {tr("aiDraft.accept")}
                    </Button>
                    <Button variant="ghost" disabled={busy} onClick={() => decide("discard")} testId="ai-draft-discard">
                      {tr("aiDraft.discard")}
                    </Button>
                  </div>
                </>
              )}
            </section>
          )}
        </div>
      </Drawer>
    </Panel>
  );
}
