"use client";
// web/components/pipeline/DraftVoicePicker.tsx — the AI draft drawer's voice picker (AA-965, card CRM-33b R41; design
// v3 A-05 voice picker). Reads the agency's voices (GET ai/voices) the first time the drawer opens and offers them with
// "No voice"; shows nothing when the agency has none or the read failed (a draft without a voice still works).
import { useEffect, useState } from "react";
import { Field, Select } from "@/app/_kit";
import { voiceOptions, type VoicesView } from "@/lib/aiExtras";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang } from "@/lib/i18n";

export function DraftVoicePicker({
  open,
  value,
  onChange,
  lang,
}: {
  open: boolean;
  value: string;
  onChange: (voiceId: string) => void;
  lang: Lang;
}) {
  const [voices, setVoices] = useState<VoicesView | null>(null);

  useEffect(() => {
    if (!open || voices) return;
    let live = true;
    void bffSend<VoicesView>("GET", "ai/voices").then((res) => {
      if (live && res.ok) setVoices(res.data);
    });
    return () => {
      live = false;
    };
  }, [open, voices]);

  const options = voiceOptions(voices);
  if (options.length === 0) return null;
  return (
    <Field label={t(lang, "aiDraft.voice")}>
      <Select value={value} onChange={(e) => onChange(e.target.value)} options={[{ value: "", label: t(lang, "aiDraft.voice.none") }, ...options]} />
    </Field>
  );
}
