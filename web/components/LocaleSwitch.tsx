"use client";
// web/components/LocaleSwitch.tsx
// AA-935 — language and date-locale picker (R32). A plain form POST to /prefs/locale (same-origin only), submitted
// on change; works without JavaScript through the button. The option labels name the language in itself and show how
// a date will look, so the choice is clear before saving.

import { K } from "@/app/_kit";
import { DATE_LOCALES, formatDate, type DateLocale } from "@/lib/dates";

const NAMES: Record<DateLocale, string> = {
  "en-AU": "English (Australia)",
  "en-GB": "English (UK)",
  "en-US": "English (US)",
  "vi-VN": "Tiếng Việt",
};

const SHORT: Record<DateLocale, string> = { "en-AU": "EN (AU)", "en-GB": "EN (UK)", "en-US": "EN (US)", "vi-VN": "Tiếng Việt" };

const SAMPLE = "2026-04-03";

export function LocaleSwitch({
  locale,
  label,
  next,
  compact = false,
  saveLabel = "OK",
}: {
  locale: DateLocale;
  label: string;
  next: string;
  compact?: boolean;
  saveLabel?: string;
}) {
  return (
    <form action="/prefs/locale" method="post" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <input type="hidden" name="next" value={next} />
      <label style={compact ? { position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" } : { fontSize: 13, color: K.body }} htmlFor={`locale-${compact ? "c" : "f"}`}>
        {label}
      </label>
      <select
        id={`locale-${compact ? "c" : "f"}`}
        name="locale"
        defaultValue={locale}
        onChange={(e) => e.currentTarget.form?.requestSubmit()}
        data-testid="locale-select"
        style={{
          padding: "6px 8px",
          borderRadius: 8,
          border: `1px solid ${K.line}`,
          background: K.card,
          color: K.ink3,
          fontSize: 12.5,
          maxWidth: compact ? 130 : 280,
        }}
      >
        {DATE_LOCALES.map((l) => (
          <option key={l} value={l}>
            {compact ? SHORT[l] : `${NAMES[l]} · ${formatDate(SAMPLE, l)}`}
          </option>
        ))}
      </select>
      <noscript>
        <button type="submit">{saveLabel}</button>
      </noscript>
    </form>
  );
}
