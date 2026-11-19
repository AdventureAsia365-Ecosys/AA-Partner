"use client";
// web/components/settings/TripPlannerModuleView.tsx — Settings > Add-ons > Trip planner (AA-1139): the planner's
// address and own domains (copy, open), where its leads go (the CRM while the agency has it, else e-mail / webhook with
// CIS's checks), its look (brand colours) and language, the webhook signing key (shown once at creation), this month's
// use (AI sessions against the plan, leads) and the newest leads with their delivery status. Data: the server page
// (GET /crm/v1/trip-planner, GET /crm/v1/trip-planner/leads); changes through the BFF (lib/bff.ts), owner only (the
// API refuses the others; the view shows them read-only). Kit: Section, Field, Input, Select, Radio, Button, Badge,
// KpiTile, DataTable.
import type { ColumnDef } from "@tanstack/react-table";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, ConfirmModal, DataTable, EmptyState, Field, Input, K, KpiTile, Radio, Section, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { type DateLocale, formatDate } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  domainTone,
  EDITED_TOKENS,
  type LeadChannel,
  leadTone,
  type PlannerLead,
  type PlannerLeadList,
  type PlannerPage,
  plannerLinks,
  type PlannerLocale,
  refusalField,
  refusalKey,
  type RotatedKey,
  routeProblem,
  sessionsTone,
  themeToSend,
  type ThemeToken,
  type TripPlannerModule,
} from "@/lib/tripPlannerModule";

const small = { margin: 0, fontSize: 12.5, color: K.muted, lineHeight: 1.55 } as const;
type Msg = { tone: "danger" | "success"; text: string } | null;

function Message({ msg, testId }: { msg: Msg; testId: string }) {
  if (!msg) return null;
  return (
    <p role={msg.tone === "danger" ? "alert" : "status"} data-testid={testId}
       style={{ margin: 0, fontSize: 12.5, color: msg.tone === "danger" ? K.danger : K.success }}>
      {msg.text}
    </p>
  );
}

function CopyButton({ text, lang }: { text: string; lang: Lang }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button variant="secondary" size="sm" onClick={() => {
      void navigator.clipboard.writeText(text).then(() => setCopied(true), () => setCopied(false));
    }}>
      {copied ? t(lang, "tp.site.copied") : t(lang, "tp.site.copy")}
    </Button>
  );
}

function SiteSection({ site, lang }: { site: TripPlannerModule; lang: Lang }) {
  const links = plannerLinks(site);
  return (
    <Section title={t(lang, "tp.site.title")} description={t(lang, "tp.site.about")}>
      <div style={{ display: "grid", gap: 10 }} data-testid="tp-site">
        {links.length === 0 && <p style={small}>{t(lang, "tp.site.none")}</p>}
        {links.map((url) => (
          <div key={url} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <code style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere", flex: "1 1 220px" }} data-testid="tp-site-url">{url}</code>
            <CopyButton text={url} lang={lang} />
            <a href={url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 13 }}>{t(lang, "tp.site.open")}</a>
          </div>
        ))}
        {site.domains.length > 0 && (
          <Field label={t(lang, "tp.site.domains")} as="div">
            <div style={{ display: "grid", gap: 6 }}>
              {site.domains.map((d) => (
                <span key={d.domain} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                  <span style={{ overflowWrap: "anywhere" }}>{d.domain}</span>
                  <Badge tone={domainTone(d.status)} dot>{t(lang, `tp.domain.${d.status}` as MessageKey)}</Badge>
                </span>
              ))}
            </div>
          </Field>
        )}
      </div>
    </Section>
  );
}

function SaveButton({ show, busy, lang, onClick, testId, label = "tp.save" }: {
  show: boolean; busy: boolean; lang: Lang; onClick: () => void; testId: string; label?: MessageKey;
}) {
  const hydrated = useHydrated();
  if (!show) return null;
  return (
    <span data-testid={testId}>
      <Button variant="primary" size="sm" disabled={!hydrated || busy} onClick={onClick}>
        {busy ? t(lang, "tp.saving") : t(lang, label)}
      </Button>
    </span>
  );
}

function CrmNote({ crm, lang }: { crm: boolean; lang: Lang }) {
  if (!crm) return null;
  return (
    <>
      <p style={{ ...small, color: K.ink }}>{t(lang, "tp.route.crm")}</p>
      <p style={small}>{t(lang, "tp.route.fallback")}</p>
    </>
  );
}

function RouteTarget({ channel, value, error, disabled, lang, onChange }: {
  channel: LeadChannel; value: string; error: string | undefined; disabled: boolean; lang: Lang; onChange: (v: string) => void;
}) {
  if (channel === "none") return null;
  const email = channel === "email";
  return (
    <Field label={t(lang, email ? "tp.route.email" : "tp.route.webhook")}
           hint={t(lang, email ? "tp.route.emailHint" : "tp.route.webhookHint")} error={error}>
      <Input type={email ? "email" : "url"} value={value} maxLength={email ? 254 : 500} disabled={disabled}
             placeholder={email ? undefined : "https://"} data-testid={email ? "tp-route-email" : "tp-route-url"}
             onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

function routeNow(route: TripPlannerModule["lead_route"], lang: Lang) {
  const label = t(lang, `tp.channel.${route.effective_channel}` as MessageKey);
  return (
    <span data-testid="tp-route-now">
      <Badge tone={route.effective_channel === "none" ? "danger" : "success"} dot>{fmt(t(lang, "tp.route.now"), { channel: label })}</Badge>
    </span>
  );
}

/** One PUT of the module through the BFF: the message to show, and whether it was saved. */
async function putModule(path: string, body: unknown, lang: Lang, okKey: MessageKey): Promise<{ msg: Msg; detail: string | null }> {
  const res = await bffSend("PUT", path, body, { renew: true });
  if (res.ok) return { msg: { tone: "success", text: t(lang, okKey) }, detail: null };
  if (res.redirecting) return { msg: null, detail: null };
  return { msg: { tone: "danger", text: t(lang, refusalKey(res.detail)) }, detail: res.detail };
}

function RouteSection({ site, owner, lang, onSaved }: { site: TripPlannerModule; owner: boolean; lang: Lang; onSaved: () => void }) {
  const route = site.lead_route;
  const [channel, setChannel] = useState<LeadChannel>(route.lead_channel);
  const [targets, setTargets] = useState({ webhook: route.lead_webhook_url ?? "", email: route.lead_email ?? "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const [field, setField] = useState<string | null>(null);

  async function save() {
    const local = routeProblem(channel, targets.webhook, targets.email);
    if (local) {
      setMsg({ tone: "danger", text: t(lang, local) });
      setField(refusalField(local.replace("tp.err.", "")));
      return;
    }
    setBusy(true);
    const out = await putModule("trip-planner/lead-route", {
      lead_channel: channel, lead_webhook_url: targets.webhook.trim() || null, lead_email: targets.email.trim() || null,
    }, lang, "tp.saved");
    setBusy(false);
    setMsg(out.msg);
    setField(out.detail ? refusalField(out.detail) : null);
    if (out.msg?.tone === "success") onSaved();
  }

  const target = channel === "webhook" ? "webhook" : "email";
  const fieldName = channel === "webhook" ? "lead_webhook_url" : "lead_email";
  return (
    <Section title={t(lang, "tp.route.title")} description={t(lang, "tp.route.about")} actions={routeNow(route, lang)}>
      <div style={{ display: "grid", gap: 12 }} data-testid="tp-route">
        <CrmNote crm={site.crm_core} lang={lang} />
        <Field label={t(lang, "tp.route.channel")} as="div">
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            {(["email", "webhook", "none"] as const).map((c) => (
              <Radio key={c} name="tp-channel" value={c} checked={channel === c} disabled={!owner || busy}
                     label={t(lang, `tp.channel.${c}` as MessageKey)} onChange={() => { setChannel(c); setMsg(null); }} />
            ))}
          </div>
        </Field>
        <RouteTarget channel={channel} value={targets[target]} disabled={!owner || busy} lang={lang}
                     error={field === fieldName ? msg?.text : undefined}
                     onChange={(v) => { setTargets({ ...targets, [target]: v }); setMsg(null); setField(null); }} />
        <SaveButton show={owner} busy={busy} lang={lang} onClick={() => void save()} testId="tp-route-save" />
        {!field && <Message msg={msg} testId="tp-route-message" />}
      </div>
    </Section>
  );
}

function LookSection({ site, owner, lang, onSaved }: { site: TripPlannerModule; owner: boolean; lang: Lang; onSaved: () => void }) {
  const hydrated = useHydrated();
  const [colours, setColours] = useState<Partial<Record<ThemeToken, string>>>(
    Object.fromEntries(EDITED_TOKENS.map((k) => [k, site.theme[k] ?? ""])));
  const [locale, setLocale] = useState<PlannerLocale>(site.locale);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const tr = (key: MessageKey) => t(lang, key);

  async function save(theme: Record<string, string>) {
    setBusy(true);
    setMsg(null);
    const res = await bffSend("PUT", "trip-planner/appearance", { theme, locale }, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setMsg({ tone: "danger", text: tr(refusalKey(res.detail)) });
      return;
    }
    setMsg({ tone: "success", text: tr("tp.look.saved") });
    onSaved();
  }

  const bad = EDITED_TOKENS.some((k) => (colours[k] ?? "") !== "" && !/^#[0-9a-fA-F]{6}$/.test(colours[k] ?? ""));
  return (
    <Section title={tr("tp.look.title")} description={tr("tp.look.about")}>
      <div style={{ display: "grid", gap: 12 }} data-testid="tp-look">
        {EDITED_TOKENS.map((token) => (
          <Field key={token} label={tr(token === "accent" ? "tp.look.accent" : "tp.look.accentDeep")} hint={tr("tp.look.colourHint")}>
            <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span aria-hidden style={{ width: 22, height: 22, borderRadius: 6, border: `1px solid ${K.line}`, flexShrink: 0,
                                         background: /^#[0-9a-fA-F]{6}$/.test(colours[token] ?? "") ? colours[token] : "transparent" }} />
              <Input value={colours[token] ?? ""} maxLength={7} placeholder="#2a6f97" disabled={!owner || busy}
                     data-testid={`tp-look-${token}`}
                     onChange={(e) => { setColours({ ...colours, [token]: e.target.value.trim() }); setMsg(null); }} />
            </span>
          </Field>
        ))}
        <Field label={tr("tp.look.locale")}>
          <Select value={locale} disabled={!owner || busy} data-testid="tp-look-locale"
                  options={[{ value: "en", label: tr("tp.look.en") }, { value: "vi", label: tr("tp.look.vi") }]}
                  onChange={(e) => { setLocale(e.target.value === "vi" ? "vi" : "en"); setMsg(null); }} />
        </Field>
        {owner && (
          <span style={{ display: "flex", gap: 8, flexWrap: "wrap" }} data-testid="tp-look-save">
            <Button variant="primary" size="sm" disabled={!hydrated || busy || bad}
                    onClick={() => void save(themeToSend(site.theme, colours))}>
              {busy ? tr("tp.saving") : tr("tp.save")}
            </Button>
            <Button variant="secondary" size="sm" disabled={!hydrated || busy}
                    onClick={() => { setColours({}); void save({}); }}>
              {tr("tp.look.reset")}
            </Button>
          </span>
        )}
        {bad && <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{tr("tp.err.theme")}</p>}
        <Message msg={msg} testId="tp-look-message" />
      </div>
    </Section>
  );
}

function KeyLine({ version, at, known, lang, locale }: { version: number; at: string | null; known: boolean; lang: Lang; locale: DateLocale }) {
  if (!known) return <p style={small}>{t(lang, "tp.unavailable")}</p>;
  const text = version > 0 && at ? fmt(t(lang, "tp.key.version"), { version, date: formatDate(at, locale) }) : t(lang, "tp.key.never");
  return <p style={small} data-testid="tp-key-version">{text}</p>;
}

function ShownKey({ rotated, lang }: { rotated: RotatedKey | null; lang: Lang }) {
  if (!rotated) return null;
  return (
    <div role="status" style={{ padding: "10px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.accentTint, display: "grid", gap: 8 }}>
      <p style={{ ...small, color: K.ink }}>{t(lang, "tp.key.once")}</p>
      <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <code style={{ fontSize: 12.5, overflowWrap: "anywhere", flex: "1 1 260px" }} data-testid="tp-key-value">{rotated.key}</code>
        <CopyButton text={rotated.key} lang={lang} />
      </span>
    </div>
  );
}

function KeyAction({ owner, available, known, label, lang, onClick }: {
  owner: boolean; available: boolean; known: boolean; label: string; lang: Lang; onClick: () => void;
}) {
  const hydrated = useHydrated();
  if (!owner) return null;
  if (!available) return <p style={small}>{t(lang, "tp.key.unavailable")}</p>;
  return (
    <span data-testid="tp-key-rotate">
      <Button variant="secondary" size="sm" disabled={!hydrated || !known} onClick={onClick}>{label}</Button>
    </span>
  );
}

/** What the key section shows: the key just created wins over the page's info (TripPlanner's, null when down). */
function keyState(info: PlannerPage["webhook_key"], shown: RotatedKey | null) {
  if (shown) return { version: shown.version, at: shown.rotated_at as string | null, known: true, available: true };
  if (!info) return { version: 0, at: null, known: false, available: true };
  return { version: info.version, at: info.rotated_at, known: true, available: info.available };
}

function KeySection({ page, owner, lang, locale }: { page: PlannerPage; owner: boolean; lang: Lang; locale: DateLocale }) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [shown, setShown] = useState<RotatedKey | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const key = keyState(page.webhook_key, shown);
  const label = t(lang, key.version > 0 ? "tp.key.rotate" : "tp.key.create");

  async function rotate() {
    setBusy(true);
    setMsg(null);
    const res = await bffSend<RotatedKey>("POST", "trip-planner/webhook-key", undefined, { renew: true });
    setBusy(false);
    setConfirming(false);
    if (res.ok) setShown(res.data);
    else if (!res.redirecting) setMsg({ tone: "danger", text: t(lang, refusalKey(res.detail)) });
  }

  return (
    <Section title={t(lang, "tp.key.title")} description={t(lang, "tp.key.about")}>
      <div style={{ display: "grid", gap: 12 }} data-testid="tp-key">
        <KeyLine version={key.version} at={key.at} known={key.known} lang={lang} locale={locale} />
        <ShownKey rotated={shown} lang={lang} />
        <KeyAction owner={owner} available={key.available} known={key.known} label={label} lang={lang}
                   onClick={() => setConfirming(true)} />
        <ConfirmModal
          open={confirming}
          onClose={() => setConfirming(false)}
          onConfirm={() => void rotate()}
          title={label}
          body={t(lang, "tp.key.confirm")}
          confirmLabel={busy ? t(lang, "tp.key.creating") : label}
          cancelLabel={t(lang, "tp.cancel")}
          destructive={key.version > 0}
          busy={busy}
          testId="tp-key-confirm"
        />
        <Message msg={msg} testId="tp-key-message" />
      </div>
    </Section>
  );
}

function UsageSection({ page, leads, lang }: { page: PlannerPage; leads: PlannerLeadList | null; lang: Lang }) {
  const tr = (key: MessageKey) => t(lang, key);
  const usage = page.usage;
  const sessions = usage
    ? usage.ai_sessions.quota === null
      ? fmt(tr("tp.usage.noLimit"), { used: usage.ai_sessions.used })
      : fmt(tr("tp.usage.of"), { used: usage.ai_sessions.used, quota: usage.ai_sessions.quota })
    : null;
  return (
    <Section title={tr("tp.usage.title")}>
      <div style={{ display: "grid", gap: 10 }} data-testid="tp-usage">
        <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
          <KpiTile label={tr("tp.usage.sessions")} value={sessions} valueTone={usage ? sessionsTone(usage) : undefined}
                   testId="tp-usage-sessions" />
          <KpiTile label={tr("tp.usage.leadsMonth")} value={leads ? leads.month.total : null} testId="tp-usage-leads-month" />
          <KpiTile label={tr("tp.usage.leads24h")} value={usage ? usage.leads_24h.count : null} testId="tp-usage-leads-24h" />
        </div>
        {!usage && <p style={small}>{tr("tp.unavailable")}</p>}
        {usage && sessionsTone(usage) === "danger" && <p role="status" style={{ ...small, color: K.danger }}>{tr("tp.usage.full")}</p>}
      </div>
    </Section>
  );
}

function LeadsSection({ leads, lang, locale }: { leads: PlannerLeadList | null; lang: Lang; locale: DateLocale }) {
  const tr = (key: MessageKey) => t(lang, key);
  const columns: ColumnDef<PlannerLead, unknown>[] = [
    { id: "when", header: tr("tp.leads.when"), accessorFn: (l) => l.created_at,
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}>{formatDate(row.original.created_at, locale)}</span> },
    { id: "channel", header: tr("tp.leads.channel"), accessorFn: (l) => tr(`tp.channel.${l.channel}` as MessageKey) },
    { id: "status", header: tr("tp.leads.status"), accessorFn: (l) => l.status,
      cell: ({ row }) => <Badge tone={leadTone(row.original.status)} dot>{tr(`tp.lead.${row.original.status}` as MessageKey)}</Badge> },
    { id: "tries", header: tr("tp.leads.tries"), accessorFn: (l) => l.attempts, meta: { align: "right" } },
    { id: "error", header: tr("tp.leads.error"), accessorFn: (l) => l.error ?? "",
      cell: ({ row }) => <code style={{ fontSize: 12 }}>{row.original.error ?? ""}</code> },
    { id: "id", header: tr("tp.leads.id"), accessorFn: (l) => l.lead_id,
      cell: ({ row }) => <code style={{ fontSize: 12 }}>{row.original.lead_id.slice(0, 8)}</code> },
  ];
  return (
    <Section title={tr("tp.leads.title")} description={tr("tp.leads.about")}>
      <div data-testid="tp-leads">
        {!leads ? (
          <p style={small}>{tr("tp.unavailable")}</p>
        ) : leads.leads.length === 0 ? (
          <EmptyState title={tr("tp.leads.empty")} />
        ) : (
          <DataTable<PlannerLead> data={leads.leads} columns={columns} tableId="tp-leads" getRowId={(l) => l.lead_id}
                                  rowTestId={(l) => `tp-lead-${l.status}`} compact paginate={false} />
        )}
      </div>
    </Section>
  );
}

export function TripPlannerModuleView({ page, leads, owner, lang, locale }: {
  page: PlannerPage; leads: PlannerLeadList | null; owner: boolean; lang: Lang; locale: DateLocale;
}) {
  const router = useRouter();
  const planner = page.module;
  const refresh = () => router.refresh();
  if (!planner.active) {
    return <p data-testid="tp-not-active" style={{ margin: 0, fontSize: 13 }}>{t(lang, "tp.notActive")}</p>;
  }
  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="tp-module">
      {!planner.in_service && <p role="status" style={{ ...small, color: K.ink }}>{t(lang, "tp.notInService")}</p>}
      {!owner && <p data-testid="tp-read-only" style={small}>{t(lang, "tp.readOnly")}</p>}
      <SiteSection site={planner} lang={lang} />
      <UsageSection page={page} leads={leads} lang={lang} />
      <RouteSection site={planner} owner={owner} lang={lang} onSaved={refresh} />
      <LookSection site={planner} owner={owner} lang={lang} onSaved={refresh} />
      <KeySection page={page} owner={owner} lang={lang} locale={locale} />
      <LeadsSection leads={leads} lang={lang} locale={locale} />
    </div>
  );
}
