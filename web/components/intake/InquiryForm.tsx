"use client";
// web/components/intake/InquiryForm.tsx — the C-01 form (AA-936). Stage 1 only (spec §5): trip basics as bands and
// codes, the prospect's name, e-mail and optional phone, companions by name only and collapsed (Fix E5), the contact
// consent box (required; without it the API stores nothing) and the optional marketing box. A hidden honeypot field
// (name from the form, off-screen, not focusable) catches bots: the API answers them as if it stored the inquiry.
// The page's random submission id makes a double click or a retry store one inquiry.
// Server-rendered: until React hydrates, a click on a destination chip is lost and the submit button would post the
// form natively (the answers in the URL). So the submit stays disabled until hydration and the form carries
// `data-hydrated="true"` once its handlers are attached (AA-936; E2E specs wait for it).
// AA-943 (CRM-15): the fields follow the API's page grouping (trip, who is travelling, about you) as three numbered
// sections on one page: the form is embedded in the agency's website as one iframe, so it stays one scroll rather than
// a wizard; a refused answer scrolls to and focuses the first refused field in page order. The optional call-slot
// picker (R15) lists the agency's free call starts in the visitor's own timezone; the value sent is the API's UTC time.
// AA-962 (errata S247 CRM-31 controller settings): the contact box shows the agency's own consent wording when it set
// one, the privacy link falls back to the agency's (the API resolves it) and the agency's data-request contact shows.
import { useMemo, useRef, useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import type { Lang, MessageKey } from "@/lib/i18n";
import { fmt } from "@/lib/i18n";
import { consentLabel, dataRequestHref, firstBadField, parseAges, slotOptions, type PublicForm } from "@/lib/intakeForms";

type Dict = Record<MessageKey, string>;
type Status = "idle" | "sending" | "sent";

const label: React.CSSProperties = { display: "grid", gap: 4, fontSize: 13, color: K.ink2, minWidth: 0 };
const input: React.CSSProperties = {
  font: "inherit",
  fontSize: 14,
  padding: "8px 10px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  minWidth: 0,
  width: "100%",
  boxSizing: "border-box",
};
const hidden: React.CSSProperties = { position: "absolute", left: "-10000px", width: 1, height: 1, overflow: "hidden" };
const legend: React.CSSProperties = { fontSize: 12, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: K.muted, marginBottom: 8 };

function options(form: PublicForm, name: string): string[] {
  return form.fields.find((f) => f.name === name)?.options ?? [];
}

export function InquiryForm({ publicKey, form, lang, dict }: { publicKey: string; form: PublicForm; lang: Lang; dict: Dict }) {
  const t = (k: MessageKey) => dict[k] ?? k;
  const code = (prefix: string, c: string) => dict[`${prefix}.${c}` as MessageKey] ?? c;
  const submissionId = useMemo(() => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : ""), []);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [bad, setBad] = useState<string[]>([]);
  const [destinations, setDestinations] = useState<string[]>([]);
  const [companions, setCompanions] = useState<string[]>([""]);
  const [childAges, setChildAges] = useState("");
  const hydrated = useHydrated();
  const formRef = useRef<HTMLFormElement>(null);
  const slots = form.call_slots ?? [];

  if (status === "sent") {
    return (
      <div data-testid="intake-thanks" role="status">
        <h2 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>{t("intake.thanks")}</h2>
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>{fmt(t("intake.thanksBody"), { agency: form.agency_name })}</p>
      </div>
    );
  }

  function invalid(name: string): React.CSSProperties {
    return bad.includes(name) ? { borderColor: K.danger } : {};
  }

  /** Mark the refused fields and bring the first one (in page order) into view, focused. */
  function refuse(fields: string[], message: string) {
    setBad(fields);
    setError(message);
    const first = firstBadField(form, fields);
    const box = first ? formRef.current?.querySelector<HTMLElement>(`[data-field="${first}"]`) : null;
    if (box) {
      box.scrollIntoView?.({ block: "center", behavior: "smooth" });
      (box.matches("input, select, textarea, button") ? box : box.querySelector<HTMLElement>("input, select, textarea, button"))?.focus({ preventScroll: true });
    }
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const text = (n: string) => String(data.get(n) ?? "").trim();
    const ages = childAges.trim() ? parseAges(childAges) : [];
    if (ages === null) {
      refuse(["child_ages"], t("intake.invalid"));
      return;
    }
    const body: Record<string, unknown> = {
      destinations,
      month: text("month") || null,
      nights: text("nights") ? Number(text("nights")) : null,
      date_flex_days: text("date_flex_days") ? Number(text("date_flex_days")) : 0,
      adults: Number(text("adults") || "1"),
      child_ages: ages,
      party_type: text("party_type") || null,
      budget_band: text("budget_band") || null,
      trip_style: text("trip_style") || null,
      name: text("name"),
      email: text("email"),
      phone: text("phone") || null,
      preferred_channel: text("preferred_channel") || null,
      heard_from: text("heard_from") || null,
      message: text("message") || null,
      companions: companions.map((c) => c.trim()).filter(Boolean),
      call_slot: text("call_slot") || null,
      consent_contact: data.get("consent_contact") === "on",
      consent_marketing: data.get("consent_marketing") === "on",
      locale: lang,
      client_submission_id: submissionId,
      [form.honeypot_field]: text(form.honeypot_field),
    };
    setStatus("sending");
    setError(null);
    setBad([]);
    try {
      const res = await fetch(`/crm/public/v1/forms/${encodeURIComponent(publicKey)}/submissions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status === 202) {
        setStatus("sent");
        return;
      }
      const payload = (await res.json().catch(() => ({}))) as { detail?: string; fields?: string[] };
      setStatus("idle");
      if (payload.detail === "consent_required") {
        refuse(["consent_contact"], t("intake.consentRequired"));
      } else if (payload.detail === "invalid_submission") {
        refuse(payload.fields ?? [], t("intake.invalid"));
      } else if (res.status === 429) {
        setError(t("intake.rateLimited"));
      } else {
        setError(t("intake.error"));
      }
    } catch {
      setStatus("idle");
      setError(t("intake.error"));
    }
  }

  const sending = status === "sending";
  const viewerZone = hydrated ? Intl.DateTimeFormat().resolvedOptions().timeZone : "UTC";
  return (
    <form ref={formRef} onSubmit={submit} noValidate data-testid="intake-form" data-hydrated={hydrated ? "true" : undefined} style={{ display: "grid", gap: 14, position: "relative" }}>
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 12 }} data-page="trip">
        <legend style={legend}>1 · {t("intake.trip")}</legend>
        <div role="group" aria-label={t("intake.destinations")} style={{ display: "grid", gap: 6 }} data-field="destinations">
          <span style={{ fontSize: 13, color: K.ink2 }}>
            {t("intake.destinations")} <span style={{ color: K.danger }}>*</span>
          </span>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, ...(bad.includes("destinations") ? { outline: `1px solid ${K.danger}`, borderRadius: 8 } : {}) }}>
            {options(form, "destinations").map((d) => {
              const on = destinations.includes(d);
              return (
                <button
                  key={d}
                  type="button"
                  aria-pressed={on}
                  data-testid={`intake-dest-${d}`}
                  onClick={() => setDestinations(on ? destinations.filter((x) => x !== d) : [...destinations, d])}
                  style={{
                    font: "inherit",
                    fontSize: 13,
                    padding: "6px 12px",
                    borderRadius: 999,
                    cursor: "pointer",
                    border: `1px solid ${on ? K.accent : K.line2}`,
                    background: on ? K.accentTint : K.card,
                    color: on ? K.accentDeep : K.ink2,
                  }}
                >
                  {code("dest", d)}
                </button>
              );
            })}
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
          <label style={label} data-field="month">
            {t("intake.month")}
            <input type="month" name="month" style={{ ...input, ...invalid("month") }} />
          </label>
          <label style={label} data-field="nights">
            {t("intake.nights")}
            <input type="number" name="nights" min={1} max={90} inputMode="numeric" style={{ ...input, ...invalid("nights") }} />
          </label>
          <label style={label} data-field="date_flex_days">
            {t("intake.flex")}
            <input type="number" name="date_flex_days" min={0} max={60} inputMode="numeric" style={{ ...input, ...invalid("date_flex_days") }} />
          </label>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
          <Select name="budget_band" text={t("intake.budget")} items={options(form, "budget_band")} prefix="budget" code={code} choose={t("intake.choose")} required invalid={bad.includes("budget_band")} />
          <Select name="trip_style" text={t("intake.style")} items={options(form, "trip_style")} prefix="style" code={code} choose={t("intake.choose")} invalid={bad.includes("trip_style")} />
        </div>
      </fieldset>

      <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 12 }} data-page="party">
        <legend style={legend}>2 · {t("intake.party")}</legend>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
          <label style={label} data-field="adults">
            <span>
              {t("intake.adults")} <span style={{ color: K.danger }}>*</span>
            </span>
            <input type="number" name="adults" min={1} max={50} defaultValue={2} required inputMode="numeric" style={{ ...input, ...invalid("adults") }} />
          </label>
          <Select name="party_type" text={t("intake.partyType")} items={options(form, "party_type")} prefix="party" code={code} choose={t("intake.choose")} invalid={bad.includes("party_type")} />
        </div>
        <label style={label} data-field="child_ages">
          {t("intake.children")}
          <input
            name="child_ages_text"
            value={childAges}
            onChange={(e) => setChildAges(e.target.value)}
            inputMode="numeric"
            style={{ ...input, ...invalid("child_ages") }}
          />
          <span style={{ fontSize: 12, color: K.muted }}>{t("intake.childrenHelp")}</span>
        </label>
        <details data-testid="intake-companions" data-field="companions" open={bad.includes("companions") || undefined}>
          <summary style={{ cursor: "pointer", fontSize: 13, color: K.accentDeep }}>{t("intake.companions")}</summary>
          <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
            {companions.map((value, i) => (
              <input
                key={i}
                aria-label={fmt(t("intake.companionName"), { n: i + 1 })}
                placeholder={fmt(t("intake.companionName"), { n: i + 1 })}
                value={value}
                maxLength={120}
                onChange={(e) => setCompanions(companions.map((c, j) => (j === i ? e.target.value : c)))}
                style={{ ...input, ...invalid("companions") }}
              />
            ))}
            {companions.length < 8 && (
              <Button size="sm" variant="ghost" onClick={() => setCompanions([...companions, ""])}>
                {t("intake.addCompanion")}
              </Button>
            )}
          </div>
        </details>
      </fieldset>

      <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 12 }} data-page="contact">
        <legend style={legend}>3 · {t("intake.you")}</legend>
        <label style={label} data-field="name">
          <span>
            {t("intake.name")} <span style={{ color: K.danger }}>*</span>
          </span>
          <input name="name" autoComplete="name" required maxLength={120} style={{ ...input, ...invalid("name") }} />
        </label>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
          <label style={label} data-field="email">
            <span>
              {t("intake.email")} <span style={{ color: K.danger }}>*</span>
            </span>
            <input name="email" type="email" autoComplete="email" required style={{ ...input, ...invalid("email") }} />
          </label>
          <label style={label} data-field="phone">
            {t("intake.phone")}
            <input name="phone" type="tel" autoComplete="tel" placeholder={t("intake.phoneHelp")} style={{ ...input, ...invalid("phone") }} />
          </label>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
          <Select name="preferred_channel" text={t("intake.channel")} items={options(form, "preferred_channel")} prefix="channel" code={code} choose={t("intake.choose")} invalid={bad.includes("preferred_channel")} />
          <Select name="heard_from" text={t("intake.heard")} items={options(form, "heard_from")} prefix="heard" code={code} choose={t("intake.choose")} invalid={bad.includes("heard_from")} />
        </div>
        <label style={label} data-field="message">
          {t("intake.message")}
          <textarea name="message" rows={4} maxLength={2000} style={{ ...input, resize: "vertical", ...invalid("message") }} />
        </label>
        {slots.length > 0 && (
          <label style={label} data-field="call_slot">
            {t("intake.call")}
            {/* Times in the visitor's own zone: rendered after hydration (the server does not know it). */}
            <select name="call_slot" defaultValue="" disabled={!hydrated} data-testid="intake-call-slot" style={{ ...input, ...invalid("call_slot") }}>
              <option value="">{t("intake.callNone")}</option>
              {hydrated &&
                slotOptions(slots, lang === "vi" ? "vi-VN" : "en-AU", viewerZone).map((g) => (
                  <optgroup key={g.day} label={g.day}>
                    {g.options.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
            </select>
            {form.call_minutes ? (
              <span style={{ fontSize: 12, color: K.muted }}>{fmt(t("intake.callHelp"), { tz: viewerZone, n: form.call_minutes })}</span>
            ) : null}
          </label>
        )}
      </fieldset>

      <div style={hidden} aria-hidden="true">
        <label>
          {t("intake.honeypot")}
          <input name={form.honeypot_field} tabIndex={-1} autoComplete="off" defaultValue="" />
        </label>
      </div>

      <label data-field="consent_contact" style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13.5, color: K.ink2, ...(bad.includes("consent_contact") ? { color: K.danger } : {}) }}>
        <input type="checkbox" name="consent_contact" data-testid="intake-consent" style={{ marginTop: 3 }} />
        <span data-testid="intake-consent-text">{consentLabel(form, fmt(t("intake.consent"), { agency: form.agency_name }))}</span>
      </label>
      <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13.5, color: K.ink2 }}>
        <input type="checkbox" name="consent_marketing" style={{ marginTop: 3 }} />
        <span>{t("intake.marketing")}</span>
      </label>
      {form.privacy_notice_url && (
        <a href={form.privacy_notice_url} target="_blank" rel="noreferrer noopener" style={{ fontSize: 12.5, color: K.accentDeep }}>
          {t("intake.privacy")}
        </a>
      )}
      {form.data_request_contact && (
        <p data-testid="intake-data-requests" style={{ margin: 0, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>
          {t("intake.dataRequests")}{" "}
          {dataRequestHref(form.data_request_contact) ? (
            <a href={dataRequestHref(form.data_request_contact) ?? undefined} target="_blank" rel="noreferrer noopener" style={{ color: K.accentDeep }}>
              {form.data_request_contact}
            </a>
          ) : (
            form.data_request_contact
          )}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="intake-error" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      <div data-testid="intake-submit">
        <Button type="submit" variant="primary" disabled={sending || !hydrated}>
          {sending ? t("intake.sending") : t("intake.submit")}
        </Button>
      </div>
    </form>
  );
}

function Select({
  name,
  text,
  items,
  prefix,
  code,
  choose,
  required = false,
  invalid = false,
}: {
  name: string;
  text: string;
  items: string[];
  prefix: string;
  code: (prefix: string, c: string) => string;
  choose: string;
  required?: boolean;
  invalid?: boolean;
}) {
  return (
    <label style={label} data-field={name}>
      <span>
        {text} {required && <span style={{ color: K.danger }}>*</span>}
      </span>
      <select name={name} defaultValue="" required={required} style={{ ...input, ...(invalid ? { borderColor: K.danger } : {}) }}>
        <option value="">{choose}</option>
        {items.map((i) => (
          <option key={i} value={i}>
            {code(prefix, i)}
          </option>
        ))}
      </select>
    </label>
  );
}
