"use client";
// web/components/clients/ClientProfileView.tsx — the client profile A-07 (AA-937, card CRM-09; design v3 WF-3,
// lanes/ux-design.md WF-3). Panels: identity and contact (masked, Reveal with a reason, edit without revealing),
// consent, the F2 profile (bands and codes), health codes (with the traveller's consent), relationships (the API
// writes the inverse, R7), trips with the age band at departure (R32) and documents. Archive, merge, export and delete
// arrive with CRM-14 / CRM-31 / CRM-35.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K } from "@/app/_kit";
import { DocumentsPanel } from "@/components/clients/DocumentsPanel";
import { RevealField } from "@/components/clients/RevealField";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import {
  DIETARY,
  MEDICAL,
  MOBILITY,
  PROFILE_CHOICES,
  RELATION_KINDS,
  REVEAL_FIELDS,
  bandLabel,
  consentLabel,
  label,
  relationLabel,
  roleText,
  type ClientProfile,
  type ProfileChoice,
  type RelationKind,
} from "@/lib/clients";
import type { DateLocale } from "@/lib/dates";
import { fmt, type Lang, type MessageKey, t } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import { memberLabel, stageLabel, whenLabel, type Member } from "@/lib/pipeline";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  maxWidth: "100%",
  minWidth: 0,
};
const dl: React.CSSProperties = { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "6px 14px", margin: 0, fontSize: 13.5 };
// The F1 fields an agency member edits here (display name included); typed values are sent, never shown back.
const EDITABLE = ["display_name", "given_name", "family_name", "email", "phone", "dob", "nationality", "passport_no", "passport_expiry", "passport_country", "emergency_contact"] as const;
const CHOICE_PREFIX: Record<ProfileChoice, string> = {
  budget_band: "budget",
  pace: "style",
  group_pref: "group",
  fitness_band: "fitness",
  trek_experience: "trek",
  accommodation_tier: "tier",
  occasion: "occasion",
  budget_flex: "flex",
  decision_maker: "decider",
  booking_timeline: "timeline",
};
const PREFIX_OF_FLAG = { dietary_flags: "dietary", mobility_flags: "mobility" } as const;
const HEALTH_GROUP = { dietary: "clients.healthDietary", mobility: "clients.healthMobility", medical: "clients.healthMedical" } as const;

export function ClientProfileView({ view, members, role, lang, locale }: { view: ClientProfile; members: Member[]; role: Role; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const { traveler, profile } = view;
  const id = traveler.traveler_id;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editIdentity, setEditIdentity] = useState(false);
  const [identity, setIdentity] = useState<Record<string, string>>({});
  const [editProfile, setEditProfile] = useState(false);
  const [bands, setBands] = useState<Record<string, string>>({});
  const [editHealth, setEditHealth] = useState(false);
  const [codes, setCodes] = useState<{ dietary: string[]; mobility: string[]; medical: string[] }>({ dietary: [], mobility: [], medical: [] });
  const [consentNow, setConsentNow] = useState(false);
  const [relQuery, setRelQuery] = useState("");
  const [relFound, setRelFound] = useState<{ traveler_id: string; display_name: string }[]>([]);
  const [relPick, setRelPick] = useState("");
  const [relKind, setRelKind] = useState<RelationKind>("spouse");

  async function send(method: "POST" | "PATCH" | "PUT" | "DELETE", path: string, body?: unknown): Promise<boolean> {
    setBusy(true);
    setError(null);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setError(t(lang, res.detail === "consent_required" ? "clients.healthConsent" : res.status === 409 ? "clients.duplicate" : "clients.error.save"));
      return false;
    }
    router.refresh();
    return true;
  }

  async function findRelation() {
    const res = await bffSend<{ items: { traveler_id: string; display_name: string }[] }>("GET", `travelers?q=${encodeURIComponent(relQuery.trim())}`);
    setRelFound(res.ok ? res.data.items.filter((i) => i.traveler_id !== id) : []);
    setRelPick("");
  }

  const owner = members.find((m) => m.member_id === traveler.owner_member_id);
  return (
    <div data-testid="crm-client" data-card="AA-937" style={{ display: "grid", gap: 14, maxWidth: 1080, minWidth: 0 }}>
      <a href="/clients" style={{ fontSize: 13, color: K.accentDeep }}>
        ← {t(lang, "clients.back")}
      </a>
      <header style={{ display: "grid", gap: 6 }}>
        <h1 data-testid="client-name" style={{ margin: 0, fontFamily: "'Fahkwang', 'Poppins', Georgia, serif", fontSize: 24, fontWeight: 500, color: K.ink, overflowWrap: "anywhere" }}>
          {traveler.display_name}
        </h1>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", fontSize: 13, color: K.muted }}>
          {traveler.archived && <Badge tone="neutral">{t(lang, "clients.archivedBadge")}</Badge>}
          <span>
            {t(lang, "clients.owner")}: <span style={{ color: K.ink }}>{owner ? memberLabel(lang, owner) : t(lang, "pipeline.unassigned")}</span>
          </span>
          {traveler.lead_source && (
            <span>
              {t(lang, "clients.source")}: <span style={{ color: K.ink }}>{traveler.lead_source}</span>
            </span>
          )}
        </div>
        {error && (
          <p role="alert" style={{ margin: 0, color: K.danger, fontSize: 13 }}>
            {error}
          </p>
        )}
      </header>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 14, alignItems: "start" }}>
        <Panel title={t(lang, "clients.identity")} testId="client-identity">
          {!editIdentity ? (
            <>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
                {REVEAL_FIELDS.filter((f) => f !== "health").map((f) => (
                  <li key={f}>
                    <RevealField travelerId={id} field={f} filled={traveler.filled[f as Exclude<typeof f, "health">]} lang={lang} label={t(lang, `field.${f}` as MessageKey)} />
                  </li>
                ))}
              </ul>
              <p style={{ margin: "10px 0 8px", fontSize: 11.5, color: K.muted }}>{t(lang, "clients.identityNote")}</p>
              <Button size="sm" variant="secondary" onClick={() => setEditIdentity(true)}>
                {t(lang, "clients.edit")}
              </Button>
            </>
          ) : (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                const changes = Object.fromEntries(Object.entries(identity).filter(([, v]) => v.trim() !== ""));
                if (Object.keys(changes).length && (await send("PATCH", `travelers/${id}`, changes))) {
                  setIdentity({});
                  setEditIdentity(false);
                }
              }}
              style={{ display: "grid", gap: 8 }}
            >
              {EDITABLE.map((f) => (
                <label key={f} style={{ display: "grid", gap: 3, fontSize: 12.5, color: K.muted }}>
                  {t(lang, `field.${f}` as MessageKey)}
                  <input
                    value={identity[f] ?? ""}
                    onChange={(e) => setIdentity((s) => ({ ...s, [f]: e.target.value }))}
                    type={f === "dob" || f === "passport_expiry" ? "date" : f === "email" ? "email" : "text"}
                    maxLength={f === "nationality" || f === "passport_country" ? 2 : 300}
                    autoComplete="off"
                    style={field}
                  />
                </label>
              ))}
              <span style={{ display: "flex", gap: 8 }}>
                <Button size="sm" type="submit" variant="primary" disabled={busy}>
                  {t(lang, "clients.save")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditIdentity(false)}>
                  {t(lang, "clients.cancel")}
                </Button>
              </span>
            </form>
          )}
        </Panel>

        <Panel title={t(lang, "clients.consent")} testId="client-consent">
          <dl style={dl}>
            <dt style={{ color: K.muted }}>{t(lang, "clients.consentContact")}</dt>
            <dd style={{ margin: 0 }}>{consentLabel(lang, view.consent, "contact")}</dd>
            <dt style={{ color: K.muted }}>{t(lang, "clients.consentMarketing")}</dt>
            <dd style={{ margin: 0 }}>{consentLabel(lang, view.consent, "marketing")}</dd>
            <dt style={{ color: K.muted }}>{t(lang, "clients.consentHealth")}</dt>
            <dd style={{ margin: 0 }}>{consentLabel(lang, view.consent, "health_data")}</dd>
          </dl>
        </Panel>

        <Panel title={t(lang, "clients.profile")} testId="client-profile">
          {!editProfile ? (
            <>
              {profile ? (
                <dl style={dl}>
                  {(Object.keys(PROFILE_CHOICES) as ProfileChoice[]).map((k) => (
                    <FragmentRow key={k} term={t(lang, `profile.${k}` as MessageKey)} value={label(lang, CHOICE_PREFIX[k], profile[k] ?? null)} />
                  ))}
                  <FragmentRow term={t(lang, "profile.residence_country")} value={profile.residence_country ?? "—"} />
                  <FragmentRow term={t(lang, "profile.altitude_ok")} value={yesNo(lang, profile.altitude_ok)} />
                  <FragmentRow term={t(lang, "profile.competing_quotes")} value={yesNo(lang, profile.competing_quotes)} />
                  <FragmentRow term={t(lang, "profile.interests")} value={profile.interests.join(", ") || "—"} />
                  {(["dietary_flags", "mobility_flags"] as const).map((k) => (
                    <FragmentRow key={k} term={t(lang, `profile.${k}` as MessageKey)} value={profile[k].map((c) => label(lang, PREFIX_OF_FLAG[k], c)).join(", ") || "—"} />
                  ))}
                  <FragmentRow term={t(lang, "profile.repeat_count")} value={String(profile.repeat_count)} />
                </dl>
              ) : (
                <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "clients.noProfile")}</p>
              )}
              <p style={{ margin: "10px 0 8px", fontSize: 11.5, color: K.muted }}>{t(lang, "clients.profileNote")}</p>
              <Button size="sm" variant="secondary" onClick={() => setEditProfile(true)}>
                {t(lang, "clients.edit")}
              </Button>
            </>
          ) : (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                const changes: Record<string, unknown> = {};
                for (const [k, v] of Object.entries(bands)) {
                  if (k === "interests") changes.interests = v.split(",").map((x) => x.trim().toLowerCase().replace(/\s+/g, "_")).filter(Boolean);
                  else if (k === "altitude_ok" || k === "competing_quotes") changes[k] = v === "" ? null : v === "yes";
                  else changes[k] = v === "" ? null : k === "residence_country" ? v.toUpperCase() : v;
                }
                if (Object.keys(changes).length && (await send("PATCH", `travelers/${id}/profile`, changes))) {
                  setBands({});
                  setEditProfile(false);
                }
              }}
              style={{ display: "grid", gap: 8 }}
            >
              {(Object.keys(PROFILE_CHOICES) as ProfileChoice[]).map((k) => (
                <label key={k} style={{ display: "grid", gap: 3, fontSize: 12.5, color: K.muted }}>
                  {t(lang, `profile.${k}` as MessageKey)}
                  <select value={bands[k] ?? profile?.[k] ?? ""} onChange={(e) => setBands((s) => ({ ...s, [k]: e.target.value }))} style={field}>
                    <option value="">—</option>
                    {PROFILE_CHOICES[k].map((code) => (
                      <option key={code} value={code}>
                        {label(lang, CHOICE_PREFIX[k], code)}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
              {(["altitude_ok", "competing_quotes"] as const).map((k) => (
                <label key={k} style={{ display: "grid", gap: 3, fontSize: 12.5, color: K.muted }}>
                  {t(lang, `profile.${k}` as MessageKey)}
                  <select value={bands[k] ?? (profile?.[k] === null || profile?.[k] === undefined ? "" : profile[k] ? "yes" : "no")} onChange={(e) => setBands((s) => ({ ...s, [k]: e.target.value }))} style={field}>
                    <option value="">{t(lang, "common.unknown")}</option>
                    <option value="yes">{t(lang, "common.yes")}</option>
                    <option value="no">{t(lang, "common.no")}</option>
                  </select>
                </label>
              ))}
              <label style={{ display: "grid", gap: 3, fontSize: 12.5, color: K.muted }}>
                {t(lang, "profile.residence_country")}
                <input value={bands.residence_country ?? profile?.residence_country ?? ""} maxLength={2} onChange={(e) => setBands((s) => ({ ...s, residence_country: e.target.value }))} style={field} />
              </label>
              <label style={{ display: "grid", gap: 3, fontSize: 12.5, color: K.muted }}>
                {t(lang, "profile.interests")}
                <input value={bands.interests ?? profile?.interests.join(", ") ?? ""} onChange={(e) => setBands((s) => ({ ...s, interests: e.target.value }))} style={field} />
              </label>
              <span style={{ display: "flex", gap: 8 }}>
                <Button size="sm" type="submit" variant="primary" disabled={busy}>
                  {t(lang, "clients.save")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditProfile(false)}>
                  {t(lang, "clients.cancel")}
                </Button>
              </span>
            </form>
          )}
        </Panel>

        <Panel title={t(lang, "clients.health")} testId="client-health">
          {traveler.has_health ? (
            <RevealField travelerId={id} field="health" filled lang={lang} label={t(lang, "clients.healthOnFile")} />
          ) : (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "clients.noHealth")}</p>
          )}
          <p style={{ margin: "10px 0 8px", fontSize: 11.5, color: K.muted }}>{t(lang, "clients.healthNote")}</p>
          {!editHealth ? (
            <Button size="sm" variant="secondary" onClick={() => setEditHealth(true)}>
              {t(lang, "clients.edit")}
            </Button>
          ) : (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (await send("PUT", `travelers/${id}/health`, { ...codes, consent_recorded: consentNow })) setEditHealth(false);
              }}
              style={{ display: "grid", gap: 8 }}
            >
              {([["dietary", DIETARY], ["mobility", MOBILITY], ["medical", MEDICAL]] as const).map(([group, list]) => (
                <fieldset key={group} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "6px 10px", margin: 0 }}>
                  <legend style={{ fontSize: 12, color: K.muted }}>{t(lang, HEALTH_GROUP[group])}</legend>
                  <span style={{ display: "flex", flexWrap: "wrap", gap: "4px 12px" }}>
                    {list.map((code) => (
                      <label key={code} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12.5, color: K.body }}>
                        <input
                          type="checkbox"
                          checked={codes[group].includes(code)}
                          onChange={(e) => setCodes((c) => ({ ...c, [group]: e.target.checked ? [...c[group], code] : c[group].filter((x) => x !== code) }))}
                        />
                        {label(lang, group, code)}
                      </label>
                    ))}
                  </span>
                </fieldset>
              ))}
              <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12.5, color: K.body }}>
                <input type="checkbox" checked={consentNow} onChange={(e) => setConsentNow(e.target.checked)} data-testid="health-consent" />
                {t(lang, "clients.healthConsent")}
              </label>
              <span style={{ display: "flex", gap: 8 }}>
                <Button size="sm" type="submit" variant="primary" disabled={busy}>
                  {t(lang, "clients.saveHealth")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditHealth(false)}>
                  {t(lang, "clients.cancel")}
                </Button>
              </span>
            </form>
          )}
        </Panel>

        <Panel title={t(lang, "clients.relations")} testId="client-relations">
          {view.relations.length === 0 ? (
            <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{t(lang, "clients.noRelations")}</p>
          ) : (
            <ul style={{ listStyle: "none", margin: "0 0 12px", padding: 0, display: "grid", gap: 8 }}>
              {view.relations.map((r) => (
                <li key={r.relation_id} style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", justifyContent: "space-between", fontSize: 13.5 }}>
                  <span style={{ minWidth: 0 }}>
                    <a href={`/clients/${r.traveler_id}`} style={{ color: K.ink, overflowWrap: "anywhere" }}>
                      {r.display_name}
                    </a>{" "}
                    <span style={{ color: K.muted }}>
                      · {relationLabel(lang, r.kind)}
                      {r.trips_together ? ` · ${fmt(t(lang, "clients.together"), { n: r.trips_together })}` : ""}
                    </span>
                  </span>
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void send("DELETE", `travelers/${id}/relations/${r.relation_id}`)}>
                    {t(lang, "clients.remove")}
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <div style={{ display: "grid", gap: 6 }}>
            <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <input aria-label={t(lang, "clients.relationSearch")} placeholder={t(lang, "clients.relationSearch")} value={relQuery} onChange={(e) => setRelQuery(e.target.value)} style={{ ...field, flex: "1 1 200px" }} />
              <Button size="sm" variant="ghost" disabled={!relQuery.trim()} onClick={() => void findRelation()}>
                {t(lang, "clients.searchButton")}
              </Button>
            </span>
            {relFound.length > 0 && (
              <span style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                <select aria-label={t(lang, "clients.relationSearch")} value={relPick} onChange={(e) => setRelPick(e.target.value)} style={field}>
                  <option value="">—</option>
                  {relFound.map((c) => (
                    <option key={c.traveler_id} value={c.traveler_id}>
                      {c.display_name}
                    </option>
                  ))}
                </select>
                <select aria-label={t(lang, "clients.relationKind")} value={relKind} onChange={(e) => setRelKind(e.target.value as RelationKind)} style={field}>
                  {RELATION_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {relationLabel(lang, k)}
                    </option>
                  ))}
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={!relPick || busy}
                  onClick={async () => {
                    if (await send("POST", `travelers/${id}/relations`, { to_traveler_id: relPick, kind: relKind })) {
                      setRelFound([]);
                      setRelQuery("");
                    }
                  }}
                >
                  {t(lang, "clients.addRelation")}
                </Button>
              </span>
            )}
          </div>
        </Panel>

        <Panel title={t(lang, "clients.tripsTitle")} testId="client-trips">
          {view.trips.length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "clients.noTrips")}</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
              {view.trips.map((trip) => (
                <li key={trip.trip_id} style={{ display: "grid", gap: 2, fontSize: 13.5 }}>
                  <a href={`/pipeline/${trip.trip_id}`} style={{ color: K.ink, overflowWrap: "anywhere" }}>
                    {trip.title}
                  </a>
                  <span style={{ fontSize: 12, color: K.muted }}>
                    {[whenLabel(trip, locale), stageLabel(lang, trip.stage), roleText(lang, trip.role), trip.age_band_at_departure ? fmt(t(lang, "clients.ageAtDeparture"), { band: bandLabel(lang, trip.age_band_at_departure) }) : null]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <DocumentsPanel documents={view.documents} travelerId={id} role={role} lang={lang} locale={locale} />
      </div>
    </div>
  );
}

function FragmentRow({ term, value }: { term: string; value: string }) {
  return (
    <>
      <dt style={{ color: K.muted }}>{term}</dt>
      <dd style={{ margin: 0, overflowWrap: "anywhere" }}>{value}</dd>
    </>
  );
}

function yesNo(lang: Lang, v: boolean | null): string {
  if (v === null || v === undefined) return t(lang, "common.unknown");
  return t(lang, v ? "common.yes" : "common.no");
}
