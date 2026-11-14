"use client";
// web/components/settings/AiVoicesPanel.tsx — Settings > AI > Voices (AA-965, card CRM-33b R41; design v3 A-18 "AI" >
// Voices). The agency's writing voices for the AI reply draft: each one a name (the agency's label) plus fixed choices
// (formality, warmth, length, emoji, greeting, sign-off), never free text. Every member sees and uses every voice and
// adds their own up to the per-member limit (an admin setting the API reports); the maker or the owner edits or deletes
// one. Calls through the BFF allow-list: POST ai/voices, PATCH / DELETE ai/voices/{id}.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { aiExtraErrorKey, canAddVoice, type Descriptors, type Voice, type VoicesView } from "@/lib/aiExtras";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const note: React.CSSProperties = { margin: 0, fontSize: 13, color: K.muted, overflowWrap: "anywhere" };

type Form = { name: string; locale: "en" | "vi"; channel: "email" | "whatsapp"; descriptors: Descriptors };

function blank(view: VoicesView): Form {
  return {
    name: "",
    locale: "en",
    channel: "email",
    descriptors: Object.fromEntries(Object.entries(view.descriptors).map(([k, v]) => [k, v[0]])),
  };
}

export function AiVoicesPanel({ view, lang }: { view: VoicesView; lang: Lang }) {
  const hydrated = useHydrated();
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [form, setForm] = useState<Form>(() => blank(view));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function start(voice: Voice | null) {
    setError(null);
    setEditing(voice ? voice.voice_profile_id : "new");
    setForm(
      voice
        ? { name: voice.name, locale: voice.locale, channel: voice.channel, descriptors: { ...blank(view).descriptors, ...voice.descriptors } }
        : blank(view),
    );
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.name.trim()) {
      setError(tr("aiX.error.invalid_name"));
      return;
    }
    setBusy(true);
    setError(null);
    const body = { ...form, name: form.name.trim() };
    const res =
      editing === "new"
        ? await bffSend("POST", "ai/voices", body, { renew: true })
        : await bffSend("PATCH", `ai/voices/${editing}`, body, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(aiExtraErrorKey(res.detail)));
      return;
    }
    setEditing(null);
    router.refresh();
  }

  async function remove(voice: Voice) {
    setBusy(true);
    setError(null);
    const res = await bffSend("DELETE", `ai/voices/${voice.voice_profile_id}`, undefined, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setError(tr(aiExtraErrorKey(res.detail)));
      return;
    }
    router.refresh();
  }

  const describe = (d: Descriptors) =>
    Object.entries(d)
      .map(([k, v]) => tr(`aiX.voices.d.${k}.${v}`))
      .join(" · ");

  return (
    <Panel title={tr("aiX.voices.title")} testId="ai-voices">
      <div data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 12, minWidth: 0 }}>
        <p style={note}>{tr("aiX.voices.intro")}</p>
        <p style={note} data-testid="ai-voices-limit">
          {fmt(tr("aiX.voices.limit"), { n: view.mine, max: view.per_member_max })}
        </p>
        {view.voices.length === 0 && <p style={note}>{tr("aiX.voices.empty")}</p>}
        {view.voices.map((voice) => (
          <div
            key={voice.voice_profile_id}
            data-testid="ai-voice"
            style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", padding: 10, border: `1px solid ${K.line}`, borderRadius: 8 }}
          >
            <div style={{ display: "grid", gap: 2, flex: "1 1 240px", minWidth: 0 }}>
              <span style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                <strong style={{ fontSize: 13.5, color: K.ink, overflowWrap: "anywhere" }}>{voice.name}</strong>
                <Badge tone="neutral">{voice.locale.toUpperCase()}</Badge>
                <Badge tone="info">{tr(`aiX.voices.channel.${voice.channel}`)}</Badge>
                {voice.mine && <Badge tone="accent">{tr("aiX.voices.mine")}</Badge>}
              </span>
              <span style={note}>{describe(voice.descriptors)}</span>
            </div>
            {voice.can_edit && (
              <div style={{ display: "flex", gap: 6 }}>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => start(voice)}>
                  {tr("aiX.voices.edit")}
                </Button>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => remove(voice)}>
                  {tr("aiX.voices.delete")}
                </Button>
              </div>
            )}
          </div>
        ))}
        {editing === null ? (
          <div>
            <Button size="sm" variant="secondary" disabled={!canAddVoice(view)} onClick={() => start(null)} testId="ai-voices-add">
              {tr("aiX.voices.add")}
            </Button>
            {!canAddVoice(view) && <p style={{ ...note, marginTop: 6 }}>{tr("aiX.voices.full")}</p>}
          </div>
        ) : (
          <form onSubmit={save} style={{ display: "grid", gap: 10, minWidth: 0 }} data-testid="ai-voices-form">
            <Field label={tr("aiX.voices.name")} hint={tr("aiX.voices.nameHelp")} required>
              <Input maxLength={60} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </Field>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              <Field label={tr("aiX.voices.locale")}>
                <Select
                  value={form.locale}
                  onChange={(e) => setForm({ ...form, locale: e.target.value as Form["locale"] })}
                  options={[
                    { value: "en", label: "English" },
                    { value: "vi", label: "Tiếng Việt" },
                  ]}
                />
              </Field>
              <Field label={tr("aiX.voices.channel")}>
                <Select
                  value={form.channel}
                  onChange={(e) => setForm({ ...form, channel: e.target.value as Form["channel"] })}
                  options={(["email", "whatsapp"] as const).map((c) => ({ value: c, label: tr(`aiX.voices.channel.${c}`) }))}
                />
              </Field>
              {Object.entries(view.descriptors).map(([key, values]) => (
                <Field key={key} label={tr(`aiX.voices.d.${key}`)}>
                  <Select
                    value={form.descriptors[key] ?? values[0]}
                    onChange={(e) => setForm({ ...form, descriptors: { ...form.descriptors, [key]: e.target.value } })}
                    options={values.map((v) => ({ value: v, label: tr(`aiX.voices.d.${key}.${v}`) }))}
                  />
                </Field>
              ))}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <Button type="submit" variant="primary" disabled={busy} testId="ai-voices-save">
                {tr("aiX.voices.save")}
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => setEditing(null)}>
                {tr("aiDraft.close")}
              </Button>
            </div>
          </form>
        )}
        {error && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger, overflowWrap: "anywhere" }}>
            {error}
          </p>
        )}
      </div>
    </Panel>
  );
}
