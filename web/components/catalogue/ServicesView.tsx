"use client";
// web/components/catalogue/ServicesView.tsx — AA-1069 (crm-052): the agency's own service catalogue (the ancillaries it
// sells beside AA's tours: flights, insurance, visas, transfers, fees). The kit DataTable lists each service with its
// price per unit, how it is counted (per traveller, per room, per night...), and, for owner and agent, the supplier
// cost per unit and the group prices. Owner and agent add, edit, switch off or delete a service (the API's
// money_fields_edit; the database refuses anyone else, CRM52); the assistant reads the list without the cost. A service
// added to a trip is a copy (the builder's "From catalogue" tab): later edits never change a proposal.
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { Badge, Button, Checkbox, DataTable, Field, Input, K, Select, useHydrated, useToast } from "@/app/_kit";
import { DateInput } from "@/components/DateInput";
import { bffSend } from "@/lib/bffClient";
import { catalogueErrorKey, tiersInput, unitInput, type Category, type QtyBasis, type Service } from "@/lib/catalogue";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatMoney, seesMargin } from "@/lib/ledger";

interface SupplierOption {
  agency_supplier_id: string;
  name: string;
}

type TierRow = { min: string; max: string; net: string };

function ServiceForm({
  service,
  categories,
  bases,
  suppliers,
  lang,
  locale,
  onDone,
}: {
  service: Service | null;
  categories: Category[];
  bases: QtyBasis[];
  suppliers: SupplierOption[];
  lang: Lang;
  locale: DateLocale;
  onDone: () => void;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [tiers, setTiers] = useState<TierRow[]>(
    (service?.tiers ?? []).map((x) => ({ min: String(x.min_pax), max: x.max_pax === null ? "" : String(x.max_pax), net: x.unit_net })),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const sell = unitInput(String(data.get("unit_sell") ?? ""));
    const net = unitInput(String(data.get("unit_net") ?? ""));
    const tierList = tiersInput(tiers);
    const currency = String(data.get("currency") ?? "").trim().toUpperCase();
    if (sell === undefined || sell === null || net === undefined || tierList === null || !/^[A-Z]{3}$/.test(currency)) {
      setError(tr("services.invalid"));
      return;
    }
    const supplier = String(data.get("agency_supplier_id") ?? "");
    const body = {
      agency_supplier_id: supplier || null,
      category: String(data.get("category") ?? "fee"),
      name: String(data.get("name") ?? "").trim(),
      unit_sell: sell,
      unit_net: net,
      currency,
      qty_basis: String(data.get("qty_basis") ?? "per_person"),
      valid_from: String(data.get("valid_from") ?? "") || null,
      valid_to: String(data.get("valid_to") ?? "") || null,
      active: data.get("active") === "on",
      tiers: tierList,
    };
    setBusy(true);
    setError(null);
    const res = service ? await bffSend("PATCH", `services/${service.service_id}`, body) : await bffSend("POST", "services", body);
    setBusy(false);
    if (!res.ok) {
      setError(tr(catalogueErrorKey(res.detail)));
      return;
    }
    toast.success(tr("services.saved"));
    onDone();
    router.refresh();
  }

  return (
    <form onSubmit={save} data-testid={service ? "service-edit-form" : "service-add-form"} style={{ display: "grid", gap: 8, maxWidth: 640, minWidth: 0 }}>
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))" }}>
        <Field label={tr("services.name")} required>
          <Input name="name" required maxLength={120} defaultValue={service?.name ?? ""} data-testid="service-name" />
        </Field>
        <Field label={tr("services.category")}>
          <Select name="category" defaultValue={service?.category ?? categories[0]} options={categories.map((c) => ({ value: c, label: tr(`catalogue.category.${c}`) }))} />
        </Field>
        <Field label={tr("services.supplier")}>
          <Select
            name="agency_supplier_id"
            defaultValue={service?.agency_supplier_id ?? ""}
            options={[{ value: "", label: tr("services.noSupplier") }, ...suppliers.map((s) => ({ value: s.agency_supplier_id, label: s.name }))]}
          />
        </Field>
        <Field label={tr("services.basis")}>
          <Select name="qty_basis" defaultValue={service?.qty_basis ?? "per_person"} options={bases.map((b) => ({ value: b, label: tr(`catalogue.basis.${b}`) }))} />
        </Field>
        <Field label={tr("services.unitSell")} required>
          <Input name="unit_sell" required inputMode="decimal" defaultValue={service?.unit_sell ?? ""} data-testid="service-sell" />
        </Field>
        <Field label={tr("services.unitNet")}>
          <Input name="unit_net" inputMode="decimal" defaultValue={service?.unit_net ?? ""} data-testid="service-net" />
        </Field>
        <Field label={tr("services.currency")} required>
          <Input name="currency" required maxLength={3} defaultValue={service?.currency ?? "USD"} />
        </Field>
        <DateInput locale={locale} name="valid_from" label={tr("services.validFrom")} defaultValue={service?.valid_from ?? ""} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
        <DateInput locale={locale} name="valid_to" label={tr("services.validTo")} defaultValue={service?.valid_to ?? ""} readAs={tr("settings.readAs")} invalid={tr("settings.invalidDate")} />
      </div>
      <Checkbox name="active" defaultChecked={service?.active ?? true} label={tr("services.active")} />
      <Field as="div" label={tr("services.tiers")}>
        <div style={{ display: "grid", gap: 6 }}>
          {tiers.map((row, i) => (
            <div key={i} style={{ display: "grid", gap: 6, gridTemplateColumns: "repeat(3, minmax(0, 1fr)) auto", alignItems: "center" }}>
              {(["min", "max", "net"] as const).map((k) => (
                <Input
                  key={k}
                  size="sm"
                  inputMode={k === "net" ? "decimal" : "numeric"}
                  aria-label={tr(`services.tiers.${k}`)}
                  placeholder={tr(`services.tiers.${k}`)}
                  value={row[k]}
                  onChange={(e) => setTiers(tiers.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)))}
                />
              ))}
              <Button size="sm" variant="ghost" onClick={() => setTiers(tiers.filter((_, j) => j !== i))}>
                {tr("services.delete")}
              </Button>
            </div>
          ))}
          <div>
            <Button size="sm" variant="ghost" onClick={() => setTiers([...tiers, { min: "", max: "", net: "" }])}>
              {tr("services.tiers.add")}
            </Button>
          </div>
        </div>
      </Field>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 12.5, color: K.danger }} data-testid="service-error">
          {error}
        </p>
      )}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy}>
          {tr("services.save")}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDone}>
          {tr("services.close")}
        </Button>
      </div>
    </form>
  );
}

export function ServicesView({
  services,
  categories,
  bases,
  suppliers,
  role,
  lang,
  locale,
}: {
  services: Service[];
  categories: Category[];
  bases: QtyBasis[];
  suppliers: SupplierOption[];
  role: string;
  lang: Lang;
  locale: DateLocale;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const toast = useToast();
  const tr = (key: string) => t(lang, key as MessageKey);
  const margin = seesMargin(role);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove(service: Service) {
    setError(null);
    const res = await bffSend("DELETE", `services/${service.service_id}`);
    if (!res.ok) {
      setError(tr(catalogueErrorKey(res.detail)));
      return;
    }
    toast.success(tr("services.deleted"));
    router.refresh();
  }

  function validity(s: Service): string | null {
    if (!s.valid_from && !s.valid_to) return null;
    return `${s.valid_from ? formatDate(s.valid_from, locale) : "…"} – ${s.valid_to ? formatDate(s.valid_to, locale) : "…"}`;
  }

  const columns: ColumnDef<Service, unknown>[] = [
    {
      id: "service",
      header: tr("services.col.service"),
      accessorFn: (s) => s.name,
      cell: ({ row }) => {
        const s = row.original;
        return (
          <div style={{ minWidth: 0, display: "grid", gap: 2 }}>
            <strong style={{ overflowWrap: "anywhere" }}>{s.name}</strong>
            <span style={{ color: K.muted, fontSize: 12 }}>
              {tr(`catalogue.category.${s.category}`)} · {s.supplier_name ?? tr("services.noSupplier")}
            </span>
            {validity(s) && <span style={{ color: K.muted, fontSize: 12 }}>{validity(s)}</span>}
            {open === s.service_id && (
              <ServiceForm service={s} categories={categories} bases={bases} suppliers={suppliers} lang={lang} locale={locale} onDone={() => setOpen(null)} />
            )}
          </div>
        );
      },
    },
    { id: "price", header: tr("services.col.price"), accessorFn: (s) => Number(s.unit_sell), cell: ({ row }) => formatMoney(row.original.unit_sell, row.original.currency, locale) },
    ...(margin
      ? [
          {
            id: "cost",
            header: tr("services.col.cost"),
            enableSorting: false,
            cell: ({ row }) => (
              <span style={{ display: "grid", gap: 2 }}>
                <span>{row.original.unit_net ? formatMoney(row.original.unit_net, row.original.currency, locale) : "—"}</span>
                {row.original.tier_count > 0 && <span style={{ color: K.muted, fontSize: 12 }}>{fmt(tr("services.tiers.count"), { n: row.original.tier_count })}</span>}
              </span>
            ),
          } as ColumnDef<Service, unknown>,
        ]
      : []),
    { id: "basis", header: tr("services.col.basis"), accessorFn: (s) => tr(`catalogue.basis.${s.qty_basis}`) },
    {
      id: "status",
      header: tr("services.col.status"),
      accessorFn: (s) => (s.active ? 1 : 0),
      cell: ({ row }) => <Badge tone={row.original.active ? "success" : "neutral"}>{tr(row.original.active ? "services.active" : "services.inactive")}</Badge>,
    },
    ...(margin
      ? [
          {
            id: "actions",
            header: "",
            enableSorting: false,
            meta: { hideLabelOnCard: true },
            cell: ({ row }) => (
              <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
                <Button size="sm" disabled={!hydrated} onClick={() => setOpen(open === row.original.service_id ? null : row.original.service_id)}>
                  {tr("services.edit")}
                </Button>
                <Button size="sm" variant="danger" disabled={!hydrated} onClick={() => remove(row.original)}>
                  {tr("services.delete")}
                </Button>
              </span>
            ),
          } as ColumnDef<Service, unknown>,
        ]
      : []),
  ];

  return (
    <div style={{ display: "grid", gap: 12, minWidth: 0, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="services-view">
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      {services.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }} data-testid="services-empty">
          {tr("services.empty")}
        </p>
      ) : (
        <DataTable<Service>
          data={services}
          columns={columns}
          tableId="crm-services"
          getRowId={(s) => s.service_id}
          rowTestId={(s) => `service-${s.service_id}`}
          searchable={services.length > 8}
          paginate={false}
          compact
          tableMinWidth={620}
        />
      )}
      {!margin ? (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("services.readOnly")}</p>
      ) : adding ? (
        <ServiceForm service={null} categories={categories} bases={bases} suppliers={suppliers} lang={lang} locale={locale} onDone={() => setAdding(false)} />
      ) : (
        <div>
          <Button variant="primary" size="sm" disabled={!hydrated} onClick={() => setAdding(true)}>
            {tr("services.add")}
          </Button>
        </div>
      )}
    </div>
  );
}
