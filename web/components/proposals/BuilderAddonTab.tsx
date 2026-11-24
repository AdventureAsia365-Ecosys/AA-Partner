"use client";
// web/components/proposals/BuilderAddonTab.tsx — the builder drawer's "AA add-on" tab (AA-1152; S285 advisory PR-29
// T6, B2 option C). The optional activities AA sells on the block's day at a place of the AA tour that day (Booking's
// add-on offers, read and filtered by the API from booking.b2b.v1 GET /add-ons), the ones at the day's place first, each with the agency's suggested price per traveller
// (its markup on AA's price, else AA's suggested retail price, rounded up to its step) and, for owner and agent, AA's
// price. "Add" puts it in this block through the API's add-on write (a live Booking quote on the trip's AA tour). A
// traveller's ticked add-on arrives on the trip as its sv: code in the lead message; it is in this list on its day.
// Calls through the BFF allow-list: GET / POST proposals/{id}/blocks/{block}/aa-addons.
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { addonNoteKeys, durationText, placesText, weekdaysText, type AaAddon, type AaAddonList } from "@/lib/aaAddons";
import { bffSend } from "@/lib/bffClient";
import { builderErrorKey, type BuilderView } from "@/lib/builder";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";

const note: React.CSSProperties = { margin: 0, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" };

function AddonRow({
  addon,
  currency,
  lang,
  locale,
  busy,
  onAdd,
}: {
  addon: AaAddon;
  currency: string;
  lang: Lang;
  locale: string;
  busy: boolean;
  onAdd: (a: AaAddon) => void;
}) {
  const tr = (key: string, vars: Record<string, string> = {}) => fmt(t(lang, key as MessageKey), vars);
  const money = (v: string) => formatMoney(v, currency, locale);
  const facts = [
    durationText(addon.duration_minutes),
    tr("addon.runs", { days: weekdaysText(addon.operating_weekdays, lang === "vi" ? "vi" : "en") }),
    addon.start_times.length ? tr("addon.starts", { times: addon.start_times.join(", ") }) : null,
    addon.min_pax > 1 ? tr("addon.minPax", { n: String(addon.min_pax) }) : null,
  ].filter(Boolean);
  return (
    <li style={{ display: "flex", gap: 10, alignItems: "flex-start", justifyContent: "space-between", padding: "8px 0", borderTop: `1px solid ${K.line}` }} data-testid="builder-addon-row">
      <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
        <span style={{ fontWeight: 600, fontSize: 13.5 }}>
          {addon.title} {addon.at_day_place && <Badge tone="success">{tr("addon.atPlace")}</Badge>}
        </span>
        <span style={note}>{placesText(addon.places, tr("addon.pickup"))}</span>
        <span style={note}>{facts.join(" · ")}</span>
        <span style={{ fontSize: 12.5 }} data-testid="builder-addon-price">
          {addon.suggested_per_person
            ? `${tr("addon.suggested", { price: money(addon.suggested_per_person) })} (${tr(`addon.basis.${addon.suggested_basis}`)})`
            : tr("addon.unpriced")}
          {addon.b2b_per_person && <span style={{ color: K.muted }}> · {tr("addon.b2b", { price: money(addon.b2b_per_person) })}</span>}
        </span>
      </div>
      <Button size="sm" disabled={busy} onClick={() => onAdd(addon)} testId="builder-addon-add">
        {tr("addon.add")}
      </Button>
    </li>
  );
}

/** Why nothing can be added yet: no AA tour on the proposal, no known place of the tour that day, no day 1 date, or
 * no add-on at the tour day's places. */
function AddonNotes({ list, tr }: { list: AaAddonList; tr: (key: string) => string }) {
  return (
    <>
      {addonNoteKeys(list).map((k) => (
        <p key={k} style={note}>
          {tr(k)}
        </p>
      ))}
    </>
  );
}

export function BuilderAddonTab({
  view,
  blockId,
  lang,
  locale,
  write,
  onAdded,
}: {
  view: Pick<BuilderView, "proposal_id">;
  blockId: string;
  lang: Lang;
  locale: string;
  write: (method: "POST", path: string, body: Record<string, unknown>) => Promise<boolean>;
  onAdded: () => void;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [list, setList] = useState<AaAddonList | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const path = `proposals/${view.proposal_id}/blocks/${blockId}/aa-addons`;

  async function load() {
    setBusy(true);
    setError(null);
    const res = await bffSend<AaAddonList>("GET", path);
    setBusy(false);
    if (res.ok) setList(res.data);
    else if (!res.redirecting) setError(tr(builderErrorKey(res.detail)));
  }

  async function add(addon: AaAddon) {
    setBusy(true);
    const ok = await write("POST", path, { component_id: addon.component_id, component_kind: addon.kind });
    setBusy(false);
    if (ok) onAdded();
  }

  return (
    <div style={{ display: "grid", gap: 8 }} data-testid="builder-addons">
      <p style={note}>{tr("addon.help")}</p>
      {!list && (
        <div>
          <Button size="sm" disabled={!hydrated || busy} onClick={load} testId="builder-addon-load">
            {tr("addon.load")}
          </Button>
        </div>
      )}
      {error && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      {list && <AddonNotes list={list} tr={tr} />}
      {list && list.items.length > 0 && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }} aria-label={tr("builder.drawer.addon")}>
          {list.items.map((a) => (
            <AddonRow key={a.component_id} addon={a} currency={list.currency} lang={lang} locale={locale} busy={busy || list.needs_tour || !list.date} onAdd={add} />
          ))}
        </ul>
      )}
    </div>
  );
}
