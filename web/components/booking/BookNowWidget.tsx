"use client";
// web/components/booking/BookNowWidget.tsx — AA-1072 (item crm-02): "Book now" on the public tour page for the agency
// whose inquiry form key the page link carries (lib/bookNow.ts). It reads the agency's offer (departures, currency),
// asks the live price of a departure for the party (the agency's retail on AA's quote, computed by the API; never a
// B2B price), and books: the API makes the traveller, the trip and a one-option proposal and answers the client page
// /c/p/<token>, where the client holds, accepts and pays the deposit. A hidden honeypot field (name from the API,
// off-screen, not focusable) catches bots; the page's random submission id makes a double click one booking. An agency
// without Book now, or a tour it cannot sell online, shows only "Ask <agency>" (the agency's inquiry form).
import { useEffect, useMemo, useState } from "react";
import { Button, Checkbox, Field, Input, K, Select, Textarea, useHydrated } from "@/app/_kit";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt } from "@/lib/i18n";
import { formatAmount } from "@/lib/proposals";
import {
  BOOK_API,
  MAX_ADULTS,
  MAX_CHILDREN,
  bookErrorKey,
  childAges,
  safeLinkPath,
  widgetUrl,
  type BookingAnswer,
  type BookingPrice,
  type BookingWidget,
} from "@/lib/bookNow";

type Dict = Record<string, string>;
type Load = { state: "loading" } | { state: "ready"; w: BookingWidget } | { state: "none"; agency: string | null };

const hidden: React.CSSProperties = { position: "absolute", left: "-10000px", width: 1, height: 1, overflow: "hidden" };

export function BookNowWidget({
  formKey,
  tour,
  pageId,
  locale,
  dict,
}: {
  formKey: string;
  tour: string | null;
  pageId: string;
  locale: DateLocale;
  dict: Dict;
}) {
  const t = (k: string) => dict[k] ?? k;
  const hydrated = useHydrated();
  const submissionId = useMemo(() => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : ""), []);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [departure, setDeparture] = useState("");
  const [adults, setAdults] = useState("2");
  const [kids, setKids] = useState<string[]>([]);
  const [price, setPrice] = useState<BookingPrice | null>(null);
  const [pricing, setPricing] = useState(false);
  const [sending, setSending] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetch(widgetUrl(formKey, tour, pageId), { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(async (res) => {
        const body = (await res.json().catch(() => ({}))) as BookingWidget & { detail?: string; agency_name?: string };
        if (!live) return;
        if (res.ok && Array.isArray(body.departures)) setLoad({ state: "ready", w: body });
        else setLoad({ state: "none", agency: null });
      })
      .catch(() => live && setLoad({ state: "none", agency: null }));
    return () => {
      live = false;
    };
  }, [formKey, tour, pageId]);

  const ages = childAges(kids);
  const adultCount = Number(adults);
  const partyOk = Number.isInteger(adultCount) && adultCount >= 1 && adultCount <= MAX_ADULTS && ages !== null;
  const askHref = `/c/f/${encodeURIComponent(formKey)}`;

  if (load.state === "loading") {
    return <div data-testid="book-now-loading" style={{ minHeight: 48 }} aria-busy="true" />;
  }
  if (load.state === "none") {
    return (
      <div data-testid="book-now-ask" style={{ display: "grid", gap: 10, justifyItems: "center", textAlign: "center" }}>
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t("book.notBookable")}</p>
        <a href={askHref} style={{ color: K.accent, fontWeight: 700 }}>
          {fmt(t("book.ask"), { agency: load.agency ?? "the agency" })}
        </a>
      </div>
    );
  }
  const w = load.w;

  function party() {
    return { adults: adultCount, child_ages: ages ?? [] };
  }

  async function checkPrice() {
    if (!departure || !partyOk) return;
    setPricing(true);
    setError(null);
    setPrice(null);
    try {
      const res = await fetch(`${BOOK_API}/${encodeURIComponent(formKey)}/price`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ tour: w.tour, departure_id: departure, ...party() }),
      });
      const body = (await res.json().catch(() => ({}))) as BookingPrice & { detail?: string };
      if (res.ok) setPrice(body);
      else setError(t(bookErrorKey(body.detail, res.status)));
    } catch {
      setError(t("book.error.other"));
    }
    setPricing(false);
  }

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!price || sending) return;
    const data = new FormData(e.currentTarget);
    setSending(true);
    setError(null);
    try {
      const res = await fetch(`${BOOK_API}/${encodeURIComponent(formKey)}/bookings`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          tour: w.tour,
          departure_id: departure,
          ...party(),
          name: String(data.get("name") ?? ""),
          email: String(data.get("email") ?? ""),
          phone: String(data.get("phone") ?? "") || null,
          message: String(data.get("message") ?? "") || null,
          consent_contact: data.get("consent_contact") === "on",
          consent_marketing: data.get("consent_marketing") === "on",
          client_submission_id: submissionId,
          [w.honeypot_field]: String(data.get(w.honeypot_field) ?? ""),
        }),
      });
      const body = (await res.json().catch(() => ({}))) as BookingAnswer & { detail?: string };
      if (res.ok && body.status === "created") {
        const path = safeLinkPath(body.link_path);
        setDone(t("book.created"));
        if (path) window.location.assign(path);
        return;
      }
      if (res.ok) {
        setDone(t("book.already"));
        return;
      }
      setError(t(bookErrorKey(body.detail, res.status)));
    } catch {
      setError(t("book.error.other"));
    }
    setSending(false);
  }

  if (done) {
    return (
      <p role="status" data-testid="book-now-done" style={{ margin: 0, fontSize: 15, color: K.success, textAlign: "center" }}>
        {done}
      </p>
    );
  }
  if (w.departures.length === 0) {
    return (
      <div data-testid="book-now-ask" style={{ display: "grid", gap: 10, justifyItems: "center", textAlign: "center" }}>
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t("book.departure.none")}</p>
        <a href={askHref} style={{ color: K.accent, fontWeight: 700 }}>{fmt(t("book.ask"), { agency: w.agency_name })}</a>
      </div>
    );
  }

  const options = w.departures.map((d) => ({
    value: d.departure_id,
    label: fmt(t("book.departure.seats"), { date: formatDate(d.start_date, locale), n: d.seats_left }),
  }));
  return (
    <form
      data-testid="book-now"
      data-hydrated={hydrated ? "true" : "false"}
      onSubmit={(e) => void submit(e)}
      style={{ display: "grid", gap: 14, textAlign: "left", padding: 20, borderRadius: 12, border: `1px solid ${K.line}`, background: K.card }}
    >
      <div>
        <h2 style={{ margin: "0 0 4px", fontSize: 20, color: K.ink }}>{t("book.title")}</h2>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{fmt(t("book.lead"), { agency: w.agency_name })}</p>
      </div>
      <Field label={t("book.departure")}>
        <Select
          value={departure}
          placeholder={t("book.departure.pick")}
          options={options}
          onChange={(e) => {
            setDeparture(e.target.value);
            setPrice(null);
          }}
          data-testid="book-now-departure"
        />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <Field label={t("book.adults")}>
          <Input
            type="number"
            inputMode="numeric"
            min={1}
            max={MAX_ADULTS}
            value={adults}
            onChange={(e) => {
              setAdults(e.target.value);
              setPrice(null);
            }}
            data-testid="book-now-adults"
          />
        </Field>
      </div>
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 8 }}>
        <legend style={{ fontSize: 13, color: K.ink2, marginBottom: 4 }}>{t("book.children")}</legend>
        {kids.map((age, i) => (
          <div key={i} style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
            <Field label={fmt(t("book.childAge"), { n: i + 1 })}>
              <Input
                type="number"
                inputMode="numeric"
                min={0}
                max={17}
                value={age}
                onChange={(e) => {
                  setKids(kids.map((k, j) => (j === i ? e.target.value : k)));
                  setPrice(null);
                }}
              />
            </Field>
            <Button
              size="sm"
              onClick={() => {
                setKids(kids.filter((_, j) => j !== i));
                setPrice(null);
              }}
            >
              {t("book.removeChild")}
            </Button>
          </div>
        ))}
        {kids.length < MAX_CHILDREN && (
          <div>
            <Button size="sm" onClick={() => setKids([...kids, ""])}>
              {t("book.addChild")}
            </Button>
          </div>
        )}
      </fieldset>
      <div>
        <span data-testid="book-now-check">
          <Button disabled={!hydrated || !departure || !partyOk || pricing} onClick={() => void checkPrice()}>
            {pricing ? t("book.price.checking") : t("book.price.check")}
          </Button>
        </span>
      </div>
      {price && (
        <div data-testid="book-now-price" role="status" style={{ display: "grid", gap: 4, padding: 12, borderRadius: 10, background: K.bg }}>
          <strong style={{ fontSize: 17, color: K.ink }}>
            {fmt(t("book.price.total"), { amount: formatAmount(price.total, price.currency, locale) })}
          </strong>
          {price.lines.map((l) => (
            <span key={l.pax_type} style={{ fontSize: 13, color: K.body }}>
              {fmt(t(`book.price.line.${l.pax_type}`), { n: l.qty, amount: formatAmount(l.unit, price.currency, locale) })}
            </span>
          ))}
          {price.availability === "on_request" && <span style={{ fontSize: 12.5, color: K.muted }}>{t("book.price.onRequest")}</span>}
        </div>
      )}
      {price && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
            <Field label={t("book.name")}>
              <Input name="name" autoComplete="name" required maxLength={120} data-testid="book-now-name" />
            </Field>
            <Field label={t("book.email")}>
              <Input name="email" type="email" autoComplete="email" required maxLength={254} data-testid="book-now-email" />
            </Field>
            <Field label={t("book.phone")}>
              <Input name="phone" type="tel" autoComplete="tel" maxLength={40} />
            </Field>
          </div>
          <Field label={t("book.message")}>
            <Textarea name="message" rows={3} maxLength={2000} />
          </Field>
          <div style={hidden} aria-hidden="true">
            <label>
              {w.honeypot_field}
              <input name={w.honeypot_field} tabIndex={-1} autoComplete="off" defaultValue="" />
            </label>
          </div>
          <Checkbox name="consent_contact" required label={w.consent_text || fmt(t("book.consent"), { agency: w.agency_name })} data-testid="book-now-consent" />
          <Checkbox name="consent_marketing" label={t("book.marketing")} />
          {w.privacy_notice_url && /^https:\/\//.test(w.privacy_notice_url) && (
            <a href={w.privacy_notice_url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 13, color: K.accent }}>
              {fmt(t("book.privacy"), { agency: w.agency_name })}
            </a>
          )}
          <div>
            <span data-testid="book-now-submit">
              <Button type="submit" variant="primary" size="lg" disabled={!hydrated || sending}>
                {sending ? t("book.submitting") : t("book.submit")}
              </Button>
            </span>
          </div>
        </>
      )}
      {error && (
        <p role="alert" data-testid="book-now-error" style={{ margin: 0, fontSize: 13.5, color: K.danger }}>
          {error}
        </p>
      )}
      <a href={askHref} style={{ fontSize: 13, color: K.accent }}>
        {fmt(t("book.ask"), { agency: w.agency_name })}
      </a>
    </form>
  );
}
