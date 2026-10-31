"use client";
// web/components/proposals/BuilderAaDrawer.tsx — the builder's "+ Add" drawer (AA-949, card CRM-21a; design WF-4, Fix
// E1): AA inventory (the AA catalog cache, then Booking's bookable parts of the tour, priced by a live quote when it
// is added: Net, sell, margin and availability) or an agency item (typed, optionally from the agency's Suppliers).
// The sell is a money field (owner / agent); the assistant adds items without one. When AA cannot be reached the
// drawer says so and AA adds stay disabled (WF-4). Sold-out items cannot be added (the API answers 409).
import { useState } from "react";
import { Badge, Button, K, useHydrated } from "@/app/_kit";
import { type AaProduct, type CatalogTour } from "@/lib/aa";
import { bffSend } from "@/lib/bffClient";
import { builderErrorKey, moneyInput, TIERS, type BuilderView, type Tier } from "@/lib/builder";
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

export interface Supplier {
  agency_supplier_id: string;
  name: string;
  kind: string;
}

export function BuilderAaDrawer({
  view,
  blockId,
  canMoney,
  suppliers,
  lang,
  locale,
  write,
  onClose,
}: {
  view: BuilderView;
  blockId: string;
  canMoney: boolean;
  suppliers: Supplier[];
  lang: Lang;
  locale: string;
  /** The page's write: adds the version's seq, handles conflicts, replaces the view on success. */
  write: (method: "POST", path: string, body: Record<string, unknown>) => Promise<boolean>;
  onClose: () => void;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [tab, setTab] = useState<"aa" | "agency">("aa");
  const [query, setQuery] = useState("");
  const [tours, setTours] = useState<CatalogTour[] | null>(null);
  const [tour, setTour] = useState<CatalogTour | null>(null);
  const [product, setProduct] = useState<AaProduct | null>(null);
  const [componentId, setComponentId] = useState("");
  const [departureId, setDepartureId] = useState("");
  const [nights, setNights] = useState("");
  const [sell, setSell] = useState("");
  const [net, setNet] = useState("");
  const [title, setTitle] = useState("");
  const [tier, setTier] = useState<Tier | "">("");
  const [supplier, setSupplier] = useState("");
  const [payOnArrival, setPayOnArrival] = useState(false);
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function fail(detail: string) {
    if (detail === "booking_unavailable" || detail === "booking_not_configured") setOffline(true);
    setError(tr(builderErrorKey(detail)));
  }

  async function search() {
    setBusy(true);
    setError(null);
    const res = await bffSend<{ tours: CatalogTour[] }>("GET", `aa/catalog?${new URLSearchParams({ q: query.trim() })}`);
    setBusy(false);
    if (res.ok) setTours(res.data.tours);
    else fail(res.detail);
  }

  async function pick(next: CatalogTour) {
    setTour(next);
    setProduct(null);
    setBusy(true);
    setError(null);
    const res = await bffSend<AaProduct>("GET", `aa/products/${encodeURIComponent(next.source_id)}?currency=${view.currency}`);
    setBusy(false);
    if (!res.ok) {
      fail(res.detail);
      return;
    }
    setOffline(false);
    setProduct(res.data);
    const first = res.data.components.find((c) => c.kind === "trip_plan") ?? res.data.components[0];
    setComponentId(first?.component_id ?? "");
  }

  async function addAa() {
    const component = product?.components.find((c) => c.component_id === componentId);
    const money = moneyInput(sell);
    if (!tour || !component || money === undefined) {
      setError(tr("builder.err.generic"));
      return;
    }
    // A hotel room is quoted for its nights (the API answers 422 invalid_nights otherwise, as AA-947 lines do).
    if (component.kind === "hotel_room" && !(nights && /^\d{1,2}$/.test(nights) && Number(nights) >= 1 && Number(nights) <= 60)) {
      setError(tr("aa.error.nights"));
      return;
    }
    const body: Record<string, unknown> = {
      tour_source_id: tour.source_id,
      component_id: component.component_id,
      component_kind: component.kind,
    };
    if (departureId) body.departure_id = departureId;
    if (nights && /^\d{1,2}$/.test(nights)) body.nights = Number(nights);
    if (tier) body.tier = tier;
    if (canMoney && money !== null) body.sell_amount = money;
    setBusy(true);
    const ok = await write("POST", `proposals/${view.proposal_id}/blocks/${blockId}/aa-options`, body);
    setBusy(false);
    if (ok) onClose();
  }

  async function addAgency() {
    const sellMoney = moneyInput(sell);
    const netMoney = moneyInput(net);
    if (!title.trim() || sellMoney === undefined || netMoney === undefined) {
      setError(tr("builder.err.generic"));
      return;
    }
    const body: Record<string, unknown> = { title: title.trim(), pay_on_arrival: payOnArrival };
    if (tier) body.tier = tier;
    if (supplier) body.agency_supplier_id = supplier;
    if (canMoney && sellMoney !== null) body.sell_amount = sellMoney;
    if (canMoney && netMoney !== null) body.net_amount = netMoney;
    setBusy(true);
    const ok = await write("POST", `proposals/${view.proposal_id}/blocks/${blockId}/options`, body);
    setBusy(false);
    if (ok) onClose();
  }

  const tierSelect = (
    <label style={label}>
      {tr("builder.option.tier")}
      <select style={field} value={tier} onChange={(e) => setTier(e.target.value as Tier | "")} data-testid="builder-tier">
        <option value="">{tr("builder.option.noTier")}</option>
        {TIERS.map((x) => (
          <option key={x} value={x}>
            {tr(`builder.tier.${x}`)}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <div role="dialog" aria-label={tr("builder.add")} data-testid="builder-drawer" style={{ display: "grid", gap: 8, padding: 12, borderRadius: 8, border: `1px solid ${K.line2}`, background: K.bg }}>
      <div role="tablist" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {(["aa", "agency"] as const).map((x) => (
          <Button key={x} size="sm" variant={tab === x ? "primary" : "ghost"} onClick={() => setTab(x)}>
            {tr(`builder.drawer.${x}`)}
          </Button>
        ))}
      </div>
      {tab === "aa" && (
        <div style={{ display: "grid", gap: 8 }} data-testid="builder-aa">
          {offline && (
            <p role="status" style={{ margin: 0 }} data-testid="builder-aa-offline">
              <Badge tone="warning">{tr("builder.aa.offline")}</Badge>
            </p>
          )}
          <div style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}>
            <label style={{ ...label, flex: "1 1 200px" }}>
              {tr("aa.add.search")}
              <input style={field} value={query} maxLength={80} onChange={(e) => setQuery(e.target.value)} data-testid="builder-aa-search" />
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
                  if (next) void pick(next);
                }}
                data-testid="builder-aa-tour"
              >
                <option value="">{tr("plan.choose")}</option>
                {tours.map((x) => (
                  <option key={x.source_id} value={x.source_id}>
                    {x.title}
                    {x.duration_days ? ` · ${fmt(tr(x.duration_days === 1 ? "aa.add.days.one" : "aa.add.days"), { n: x.duration_days })}` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          {product && (
            <>
              <label style={label}>
                {tr("aa.add.component")}
                <select style={field} value={componentId} onChange={(e) => setComponentId(e.target.value)} data-testid="builder-aa-component">
                  {product.components.map((c) => (
                    <option key={c.component_id} value={c.component_id}>
                      {tr(`aa.kind.${c.kind}`)}
                      {c.day_no ? ` · ${fmt(tr("aa.add.dayNo"), { n: c.day_no })}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              {product.departures.length > 0 && (
                <label style={label}>
                  {tr("aa.add.departure")}
                  <select style={field} value={departureId} onChange={(e) => setDepartureId(e.target.value)}>
                    <option value="">{tr("aa.add.ownDate")}</option>
                    {product.departures.map((d) => (
                      <option key={d.departure_id} value={d.departure_id} disabled={d.available <= 0}>
                        {d.start_date} · {d.available > 0 ? fmt(tr(d.available === 1 ? "aa.add.seats.one" : "aa.add.seats"), { n: d.available }) : tr("builder.avail.sold_out")}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {product.components.find((c) => c.component_id === componentId)?.kind === "hotel_room" && (
                <label style={label}>
                  {tr("aa.add.nights")}
                  <input style={field} inputMode="numeric" value={nights} onChange={(e) => setNights(e.target.value)} />
                </label>
              )}
              {product.from_price && (
                <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
                  {fmt(tr("aa.fromPrice"), { price: formatMoney(product.from_price.amount, product.from_price.currency, locale) })}
                </p>
              )}
              <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("builder.aa.quoteHint")}</p>
            </>
          )}
          {tierSelect}
          {canMoney && (
            <label style={label}>
              {fmt(tr("builder.option.sellFor"), { currency: view.currency })}
              <input style={field} inputMode="decimal" value={sell} onChange={(e) => setSell(e.target.value)} data-testid="builder-aa-sell" />
            </label>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={!hydrated || busy || offline || !product || !componentId} onClick={addAa}>
              {tr("builder.aa.add")}
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              {tr("ledger.close")}
            </Button>
          </div>
        </div>
      )}
      {tab === "agency" && (
        <div style={{ display: "grid", gap: 8 }} data-testid="builder-agency">
          <label style={label}>
            {tr("builder.option.title")}
            <input style={field} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} data-testid="builder-agency-title" />
          </label>
          {suppliers.length > 0 && (
            <label style={label}>
              {tr("builder.option.supplier")}
              <select style={field} value={supplier} onChange={(e) => setSupplier(e.target.value)}>
                <option value="">{tr("builder.option.noSupplier")}</option>
                {suppliers.filter((s) => s.kind !== "aa").map((s) => (
                  <option key={s.agency_supplier_id} value={s.agency_supplier_id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {tierSelect}
          {canMoney && (
            <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
              <label style={label}>
                {fmt(tr("builder.option.sellFor"), { currency: view.currency })}
                <input style={field} inputMode="decimal" value={sell} onChange={(e) => setSell(e.target.value)} data-testid="builder-agency-sell" />
              </label>
              <label style={label}>
                {fmt(tr("builder.option.netFor"), { currency: view.currency })}
                <input style={field} inputMode="decimal" value={net} onChange={(e) => setNet(e.target.value)} data-testid="builder-agency-net" />
              </label>
            </div>
          )}
          <label style={{ display: "flex", gap: 6, fontSize: 12.5, alignItems: "center" }}>
            <input type="checkbox" checked={payOnArrival} onChange={(e) => setPayOnArrival(e.target.checked)} />
            {tr("builder.option.payOnArrival")}
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={!hydrated || busy} onClick={addAgency}>
              {tr("builder.agency.add")}
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              {tr("ledger.close")}
            </Button>
          </div>
        </div>
      )}
      {error && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.danger }} data-testid="builder-drawer-error">
          {error}
        </p>
      )}
    </div>
  );
}
