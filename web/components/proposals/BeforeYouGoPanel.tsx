"use client";
// web/components/proposals/BeforeYouGoPanel.tsx — the builder's "Before you go" day 0 (AA-951, card CRM-22; design R38,
// Q1 2.3). Lists the AA prep packs CIS publishes for the countries of the version's AA items (none without an AA item:
// card E), the packs picked for this version, and the two reminders every client text carries (travel insurance; the
// fitness warning when an AA item is a trek or high-altitude). In a draft the agent ticks packs and saves (one
// builder write with the version counter, PUT /proposals/{id}/before-you-go); a published version shows its packs read
// only. Visa text never comes from a pack: the client text keeps only the official link (the API renders it).
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { togglePack, withSuggestions, type BeforeYouGo } from "@/lib/textProposal";

export function BeforeYouGoPanel({
  data,
  draft,
  busy,
  lang,
  onSave,
}: {
  data: BeforeYouGo;
  draft: boolean;
  busy: boolean;
  lang: Lang;
  onSave: (groupKeys: string[]) => Promise<boolean>;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const saved = data.selected.map((s) => s.group_key);
  const [picks, setPicks] = useState<string[]>(saved);
  const dirty = picks.join("|") !== saved.join("|");
  // Picked packs that are no longer suggested (the AA item was removed) stay listed so the agent can untick them.
  const rows = [
    ...data.suggested.map((s) => ({ key: s.group_key, title: s.title, locales: s.locales })),
    ...data.selected.filter((s) => !s.suggested).map((s) => ({ key: s.group_key, title: s.title, locales: s.locales })),
  ];

  return (
    <section aria-labelledby="byg-heading" data-testid="builder-before-you-go" style={{ display: "grid", gap: 10, marginTop: 12 }}>
      <h3 id="byg-heading" style={{ margin: 0, fontSize: 13.5, color: K.ink }}>
        {tr("byg.packs")}
      </h3>
      {data.aa_items === 0 ? (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="byg-no-aa">
          {tr("byg.noAa")}
        </p>
      ) : rows.length === 0 ? (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="byg-none">
          {fmt(tr("byg.noPacks"), { countries: data.countries.join(", ") || "—" })}
        </p>
      ) : (
        <fieldset style={{ border: 0, margin: 0, padding: 0, display: "grid", gap: 6 }} disabled={!draft || !hydrated || busy}>
          <legend style={{ fontSize: 12.5, color: K.muted, padding: 0, marginBottom: 4 }}>
            {fmt(tr("byg.suggestedFor"), { countries: data.countries.join(", ") || "—" })}
          </legend>
          {rows.map((r) => (
            <label key={r.key} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, flexWrap: "wrap" }} data-testid="byg-pack">
              <input type="checkbox" checked={picks.includes(r.key)} onChange={(e) => setPicks(togglePack(picks, r.key, e.currentTarget.checked))} />
              <span style={{ overflowWrap: "anywhere" }}>{r.title}</span>
              <span style={{ color: K.muted, fontSize: 12 }}>{r.locales.map((l) => l.toUpperCase()).join(" · ")}</span>
            </label>
          ))}
        </fieldset>
      )}
      {draft && rows.length > 0 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <span data-testid="byg-save">
            <Button size="sm" variant="primary" disabled={!hydrated || busy || !dirty} onClick={() => void onSave(picks)}>
              {tr("byg.save")}
            </Button>
          </span>
          {data.packs_not_chosen && (
            <Button size="sm" disabled={!hydrated || busy} onClick={() => void onSave(withSuggestions(data))}>
              {tr("byg.useSuggested")}
            </Button>
          )}
        </div>
      )}
      <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 6, fontSize: 12.5 }} data-testid="byg-reminders">
        <li style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}>
          <Badge tone="info">{tr("byg.visa")}</Badge>
          <span style={{ color: K.muted }}>{tr("byg.visaHint")}</span>
        </li>
        {data.insurance_reminder && (
          <li style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}>
            <Badge tone="info">{tr("byg.insurance")}</Badge>
            <span style={{ color: K.muted }}>{tr("byg.insuranceHint")}</span>
          </li>
        )}
        {data.fitness_warning && (
          <li style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }} data-testid="byg-fitness">
            <Badge tone="warning">{tr("byg.fitness")}</Badge>
            <span style={{ color: K.muted }}>{tr("byg.fitnessHint")}</span>
          </li>
        )}
      </ul>
    </section>
  );
}
