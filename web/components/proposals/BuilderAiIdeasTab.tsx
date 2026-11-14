"use client";
// web/components/proposals/BuilderAiIdeasTab.tsx — the builder drawer's "AI ideas" tab (AA-965, card CRM-33b R40;
// design v3 A-04 "AI: ideas from call notes"; ux-design "AI ideas only from the day's AA stock, closed list of product
// codes, server re-checks with Booking"). The agent runs it for the block's day, with typed notes or the trip's own
// notes; the API offers the model only the AA products the agency can sell in the trip's countries and drops any other
// pick. Each idea shows its AA tour, part, tour day and the model's one-line reason with the staff AiNotice; "Add" puts
// it in this block through the normal AA add (a live Booking quote: the server re-check), and a hotel room asks for its
// nights. Calls through the BFF allow-list: POST proposal-days/{id}/ai/options, then the builder's aa-options write.
import { useState } from "react";
import { AiNotice, Button, Field, Input, K, Textarea } from "@/app/_kit";
import { aiExtraErrorKey, aiExtraFallbackKey, type NotesOptionsResult, type OptionIdea } from "@/lib/aiExtras";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const note: React.CSSProperties = { margin: 0, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" };
const NOTES_BOUND = 30_000; // the API's request bound; the admin setting (crm.ai.notes_options_max_chars) is checked there

export function BuilderAiIdeasTab({
  proposalId,
  dayId,
  blockId,
  lang,
  write,
  onAdded,
}: {
  proposalId: string;
  dayId: string;
  blockId: string;
  lang: Lang;
  write: (method: "POST", path: string, body: Record<string, unknown>) => Promise<boolean>;
  onAdded: () => void;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<NotesOptionsResult | null>(null);
  const [nights, setNights] = useState<Record<string, string>>({});

  async function run() {
    setBusy(true);
    setError(null);
    setResult(null);
    const res = await bffSend<NotesOptionsResult>(
      "POST",
      `proposal-days/${dayId}/ai/options`,
      notes.trim() ? { notes: notes.trim() } : {},
      { renew: true },
    );
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(aiExtraErrorKey(res.detail)));
      return;
    }
    setResult(res.data);
  }

  async function add(idea: OptionIdea) {
    const body: Record<string, unknown> = {
      tour_source_id: idea.tour_source_id,
      component_id: idea.component_id,
      component_kind: idea.component_kind,
    };
    if (idea.component_kind === "hotel_room") {
      const n = nights[idea.code] ?? "";
      if (!(/^\d{1,2}$/.test(n) && Number(n) >= 1 && Number(n) <= 60)) {
        setError(tr("aa.error.nights"));
        return;
      }
      body.nights = Number(n);
    }
    setBusy(true);
    setError(null);
    const ok = await write("POST", `proposals/${proposalId}/blocks/${blockId}/aa-options`, body);
    setBusy(false);
    if (ok) onAdded();
  }

  const usable = result && result.outcome === "ok";

  return (
    <div style={{ display: "grid", gap: 8, minWidth: 0 }} data-testid="builder-ai-ideas">
      <p style={note}>{tr("aiX.ideas.intro")}</p>
      <Field label={tr("aiX.ideas.notes")} hint={tr("aiX.ideas.notesHelp")}>
        <Textarea rows={3} maxLength={NOTES_BOUND} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </Field>
      <div>
        <Button size="sm" variant="primary" disabled={busy} onClick={run} testId="builder-ai-ideas-run">
          {busy && !result ? tr("aiX.ideas.running") : tr("aiX.ideas.run")}
        </Button>
      </div>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger, overflowWrap: "anywhere" }}>
          {error}
        </p>
      )}
      {result && !usable && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.amber }} data-testid="builder-ai-ideas-fallback">
          {tr(aiExtraFallbackKey(result))}
        </p>
      )}
      {usable && (
        <section style={{ display: "grid", gap: 8, minWidth: 0 }} data-testid="builder-ai-ideas-result" data-ai-generated="true">
          {result.options.length === 0 && <p style={note}>{tr("aiX.ideas.none")}</p>}
          {result.options.map((idea) => (
            <div
              key={idea.code}
              data-testid="builder-ai-idea"
              style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap", padding: 8, border: `1px solid ${K.line}`, borderRadius: 8 }}
            >
              <div style={{ display: "grid", gap: 2, flex: "1 1 220px", minWidth: 0 }}>
                <strong style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{idea.title}</strong>
                <span style={note}>
                  {tr(`aa.kind.${idea.component_kind}`)}
                  {idea.tour_day ? ` · ${fmt(tr("aiX.ideas.tourDay"), { n: idea.tour_day })}` : ""}
                </span>
                {idea.why && <span style={{ ...note, color: K.body }}>{idea.why}</span>}
              </div>
              {idea.component_kind === "hotel_room" && (
                <Field label={tr("aiX.ideas.nights")}>
                  <Input
                    size="sm"
                    inputMode="numeric"
                    maxLength={2}
                    value={nights[idea.code] ?? ""}
                    onChange={(e) => setNights((n) => ({ ...n, [idea.code]: e.target.value }))}
                    style={{ width: 70 }}
                  />
                </Field>
              )}
              <Button size="sm" disabled={busy} onClick={() => add(idea)}>
                {tr("aiX.ideas.add")}
              </Button>
            </div>
          ))}
          {result.rejected > 0 && <p style={note}>{fmt(tr("aiX.ideas.rejected"), { n: result.rejected })}</p>}
          <AiNotice lang={lang} />
        </section>
      )}
    </div>
  );
}
