"use client";
// web/components/catalogue/BuilderCatalogueTab.tsx — AA-1069 (crm-052): the builder drawer's "From catalogue" tab. It
// lists the agency's offered services (GET /bff/services?active=true), shows how many units the party takes (the
// quantity basis against the proposal's travellers, rooms and nights; the API recomputes it) and adds the service to
// the block as an agency option (POST proposals/{id}/blocks/{block}/catalogue-options): a copy of the price, so later
// edits of the service never change the proposal. Every role adds from the catalogue (the prices are the owner's);
// the cost per unit is shown to owner and agent only. A service not offered on the block's day is left out.
import Link from "next/link";
import { useEffect, useState } from "react";
import { Button, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import type { BuilderView } from "@/lib/builder";
import { quantityFor, validOn, type CatalogueView, type Service } from "@/lib/catalogue";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney } from "@/lib/ledger";

/** The proposal's party for the preview: the room list when it has one, else the Stage 2 party. */
export function builderParty(view: Pick<BuilderView, "room_config" | "stage2">): { travellers: number; adults: number; rooms: number } {
  const rooms = view.room_config ?? [];
  if (rooms.length > 0) {
    const adults = rooms.reduce((n, r) => n + (r.adults ?? 0), 0);
    const children = rooms.reduce((n, r) => n + (r.children ?? 0), 0);
    return { travellers: Math.max(1, adults + children), adults: Math.max(1, adults), rooms: rooms.length };
  }
  const adults = Math.max(1, view.stage2?.party_adults ?? 1);
  return { travellers: adults + (view.stage2?.party_children ?? 0), adults, rooms: 1 };
}

export function BuilderCatalogueTab({
  view,
  blockId,
  dayDate,
  canMargin,
  lang,
  locale,
  write,
  onClose,
}: {
  view: BuilderView;
  blockId: string;
  /** The block's day as an ISO date (null when day 1 has no date yet: today counts for the validity window). */
  dayDate: string | null;
  canMargin: boolean;
  lang: Lang;
  locale: string;
  write: (method: "POST", path: string, body: Record<string, unknown>) => Promise<boolean>;
  onClose: () => void;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [services, setServices] = useState<Service[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [picked, setPicked] = useState("");
  const [quantity, setQuantity] = useState("");
  const [nights, setNights] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void bffSend<CatalogueView>("GET", "services?active=true").then((res) => {
      if (!live) return;
      if (!res.ok) {
        setFailed(true);
        return;
      }
      const offered = res.data.services.filter((s) => s.active && validOn(s, dayDate));
      setServices(offered);
      setPicked(offered[0]?.service_id ?? "");
    });
    return () => {
      live = false;
    };
  }, [dayDate]);

  const service = services?.find((s) => s.service_id === picked) ?? null;
  const party = builderParty(view);
  const dayCount = Math.max(1, view.days.filter((d) => d.day_no >= 1).length - 1);
  const typedNights = /^\d{1,2}$/.test(nights) ? Number(nights) : null;
  const typedQty = /^\d{1,4}$/.test(quantity) && Number(quantity) >= 1 ? Number(quantity) : null;
  const qty = service ? (typedQty ?? quantityFor(service.qty_basis, party, typedNights ?? dayCount)) : 0;

  async function add() {
    if (!service) return;
    const body: Record<string, unknown> = { service_id: service.service_id };
    if (typedQty !== null) body.quantity = typedQty;
    if (typedNights !== null && typedNights >= 1) body.nights = typedNights;
    setBusy(true);
    const ok = await write("POST", `proposals/${view.proposal_id}/blocks/${blockId}/catalogue-options`, body);
    setBusy(false);
    if (ok) onClose();
  }

  if (failed) return <p style={{ margin: 0, fontSize: 12.5, color: K.danger }}>{tr("catalogue.error.other")}</p>;
  if (services === null) return <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>…</p>;
  if (services.length === 0) {
    return (
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="builder-catalogue-empty">
        {tr("catalogue.none")} <Link href="/services">{tr("catalogue.open")}</Link>
      </p>
    );
  }
  return (
    <div style={{ display: "grid", gap: 8, minWidth: 0 }} data-testid="builder-catalogue">
      <Field label={tr("catalogue.pick")}>
        <Select
          value={picked}
          onChange={(e) => setPicked(e.target.value)}
          data-testid="builder-catalogue-service"
          options={services.map((s) => ({
            value: s.service_id,
            label: `${s.name} · ${formatMoney(s.unit_sell, s.currency, locale)} · ${tr(`catalogue.basis.${s.qty_basis}`)}`,
          }))}
        />
      </Field>
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))" }}>
        <Field label={tr("catalogue.quantity")}>
          <Input inputMode="numeric" maxLength={4} value={quantity} onChange={(e) => setQuantity(e.target.value)} data-testid="builder-catalogue-qty" />
        </Field>
        {service && (service.qty_basis === "per_night" || service.qty_basis === "per_person_night") && (
          <Field label={tr("catalogue.nights")}>
            <Input inputMode="numeric" maxLength={2} value={nights} onChange={(e) => setNights(e.target.value)} />
          </Field>
        )}
      </div>
      {service && (
        <p style={{ margin: 0, fontSize: 13, fontVariantNumeric: "tabular-nums" }} data-testid="builder-catalogue-preview">
          {fmt(tr("catalogue.preview"), {
            qty,
            unit: formatMoney(service.unit_sell, service.currency, locale),
            total: formatMoney((Number(service.unit_sell) * qty).toFixed(2), service.currency, locale),
          })}
          {canMargin && service.unit_net ? ` · ${tr("services.unitNet")}: ${formatMoney(service.unit_net, service.currency, locale)}` : ""}
        </p>
      )}
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("catalogue.snapshot")}</p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button size="sm" variant="primary" disabled={!hydrated || busy || !service} onClick={add}>
          {tr("catalogue.add")}
        </Button>
      </div>
    </div>
  );
}
