"use client";
// web/components/ledger/AaItemForm.tsx — "Add AA item" on the trip's Bookings tab (AA-947, card CRM-19). The agent
// never types an AA line: they search the AA catalog (GET aa/catalog, the CIS cache), pick a tour, then one of its
// bookable parts and dates from Booking (GET aa/products/{id}?currency=, live), the travellers (counts only) and,
// owner / agent only, the sell amount. A sell per person below AA's public "from" price shows a warning and never
// blocks. POST trips/{id}/aa-lines then creates the line as "AA pending"; Booking's quote fills it in.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { aaErrorKey, aaLineBody, belowFromPrice, type AaProduct, type CatalogTour } from "@/lib/aa";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
  minWidth: 0,
  width: "100%",
};
const label: React.CSSProperties = { display: "grid", gap: 2, fontSize: 12 };

export function AaItemForm({
  tripId,
  baseCurrency,
  canSell,
  lang,
  locale,
  onClose,
}: {
  tripId: string;
  baseCurrency: string;
  canSell: boolean;
  lang: Lang;
  locale: DateLocale;
  onClose: () => void;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [query, setQuery] = useState("");
  const [tours, setTours] = useState<CatalogTour[] | null>(null);
  const [tour, setTour] = useState<CatalogTour | null>(null);
  const [product, setProduct] = useState<AaProduct | null>(null);
  const [componentId, setComponentId] = useState("");
  const [departureId, setDepartureId] = useState("");
  const [currency, setCurrency] = useState(baseCurrency);
  const [adults, setAdults] = useState("2");
  const [children, setChildren] = useState("0");
  const [infants, setInfants] = useState("0");
  const [nights, setNights] = useState("");
  const [sell, setSell] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    setBusy(true);
    setError(null);
    const res = await bffSend<{ tours: CatalogTour[] }>("GET", `aa/catalog?${new URLSearchParams({ q: query.trim() })}`);
    setBusy(false);
    if (res.ok) setTours(res.data.tours);
    else setError(tr(aaErrorKey(res.detail)));
  }

  async function load(next: CatalogTour, cur: string) {
    setTour(next);
    setProduct(null);
    setBusy(true);
    setError(null);
    const params = /^[A-Z]{3}$/.test(cur) ? `?currency=${cur}` : "";
    const res = await bffSend<AaProduct>("GET", `aa/products/${encodeURIComponent(next.source_id)}${params}`);
    setBusy(false);
    if (!res.ok) {
      setError(tr(aaErrorKey(res.detail)));
      return;
    }
    setProduct(res.data);
    const first = res.data.components.find((c) => c.kind === "trip_plan") ?? res.data.components[0];
    setComponentId(first?.component_id ?? "");
  }

  const component = product?.components.find((c) => c.component_id === componentId) ?? null;
  const below = canSell ? belowFromPrice(sell, currency, Number(adults) || 0, Number(children) || 0, product?.from_price ?? null) : null;

  async function submit(form: FormData) {
    const departure = product?.departures.find((d) => d.departure_id === departureId);
    const built = aaLineBody({
      tourSourceId: tour?.source_id ?? "",
      componentId,
      componentKind: component?.kind ?? "",
      departureId,
      serviceDate: departure ? departure.start_date : String(form.get("service_date") ?? ""),
      nights,
      adults,
      children,
      infants,
      currency,
      sell: canSell ? sell : "",
    });
    if (!built.ok) {
      setError(tr(built.error));
      return;
    }
    setBusy(true);
    setError(null);
    const res = await bffSend<unknown>("POST", `trips/${tripId}/aa-lines`, built.body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(aaErrorKey(res.detail)));
      return;
    }
    onClose();
    router.refresh();
  }

  return (
    <form action={submit} style={{ display: "grid", gap: 8, maxWidth: 560 }} data-testid="aa-add-form">
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("aa.add.hint")}</p>
      <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
        <label style={{ ...label, flex: "1 1 220px" }}>
          {tr("aa.add.search")}
          <input style={field} value={query} maxLength={80} onChange={(e) => setQuery(e.target.value)} data-testid="aa-search" />
        </label>
        <Button size="sm" disabled={!hydrated || busy} onClick={search}>
          {tr("aa.add.find")}
        </Button>
      </div>
      {tours && tours.length === 0 && <p style={{ margin: 0, fontSize: 12.5 }}>{tr("aa.add.noTours")}</p>}
      {tours && tours.length > 0 && (
        <label style={label}>
          {tr("aa.add.tour")}
          <select
            style={field}
            value={tour?.source_id ?? ""}
            onChange={(e) => {
              const next = tours.find((x) => x.source_id === e.target.value);
              if (next) void load(next, currency);
            }}
            data-testid="aa-tour"
          >
            <option value="">{tr("plan.choose")}</option>
            {tours.map((x) => (
              <option key={x.source_id} value={x.source_id}>
                {x.title}
                {x.duration_days ? ` · ${fmt(tr("aa.add.days"), { n: x.duration_days })}` : ""}
                {x.country_code ? ` · ${x.country_code}` : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      {product && (
        <>
          <label style={label}>
            {tr("aa.add.component")}
            <select style={field} value={componentId} onChange={(e) => setComponentId(e.target.value)} data-testid="aa-component">
              {product.components.map((c) => (
                <option key={c.component_id} value={c.component_id}>
                  {tr(`aa.kind.${c.kind}`)}
                  {c.day_no ? ` · ${fmt(tr("aa.add.dayNo"), { n: c.day_no })}` : ""}
                </option>
              ))}
            </select>
          </label>
          {product.departures.length > 0 && component?.kind === "trip_plan" && (
            <label style={label}>
              {tr("aa.add.departure")}
              <select style={field} value={departureId} onChange={(e) => setDepartureId(e.target.value)} data-testid="aa-departure">
                <option value="">{tr("aa.add.ownDate")}</option>
                {product.departures.map((d) => (
                  <option key={d.departure_id} value={d.departure_id}>
                    {formatDate(d.start_date, locale)} · {fmt(tr("aa.add.seats"), { n: d.available })}
                  </option>
                ))}
              </select>
            </label>
          )}
          {!departureId && (
            <DateInput locale={locale} name="service_date" label={tr("aa.add.date")} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
          )}
          {component?.kind === "hotel_room" && (
            <label style={label}>
              {tr("aa.add.nights")}
              <input style={field} inputMode="numeric" value={nights} onChange={(e) => setNights(e.target.value)} />
            </label>
          )}
          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))" }}>
            {(
              [
                ["aa.add.adults", adults, setAdults],
                ["aa.add.children", children, setChildren],
                ["aa.add.infants", infants, setInfants],
              ] as const
            ).map(([key, value, set]) => (
              <label key={key} style={label}>
                {tr(key)}
                <input style={field} inputMode="numeric" value={value} onChange={(e) => set(e.target.value)} />
              </label>
            ))}
            <label style={label}>
              {tr("ledger.currency")}
              <input
                style={field}
                maxLength={3}
                value={currency}
                onChange={(e) => setCurrency(e.target.value.toUpperCase())}
                onBlur={() => tour && /^[A-Z]{3}$/.test(currency) && product?.from_price?.currency !== currency && load(tour, currency)}
              />
            </label>
          </div>
          {canSell && (
            <label style={label}>
              {tr("aa.add.sell")}
              <input style={field} inputMode="decimal" value={sell} onChange={(e) => setSell(e.target.value)} data-testid="aa-sell" />
            </label>
          )}
          {product.from_price && (
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
              {fmt(tr("aa.fromPrice"), { price: formatMoney(product.from_price.amount, product.from_price.currency, locale) })}
            </p>
          )}
          {below && (
            <p role="status" style={{ margin: 0 }} data-testid="aa-from-warning">
              <Badge tone="warning">{tr("aa.belowFrom")}</Badge>
            </p>
          )}
        </>
      )}
      {error && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.danger }} data-testid="aa-add-error">
          {error}
        </p>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy || !product || !componentId}>
          {tr("aa.add.submit")}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose}>
          {tr("ledger.close")}
        </Button>
      </div>
    </form>
  );
}
