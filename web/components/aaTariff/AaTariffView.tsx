"use client";
// web/components/aaTariff/AaTariffView.tsx — AA-1030: the agency's AA prices (lib/aaTariff.ts). The toolbar (currency,
// Download CSV through the BFF with AA's own file name, Print) and the table on the kit DataTable (a card per row on a
// phone, every row on one page so Print prints them all; its search box narrows the rows, and Print then prints those,
// the box itself hidden by the print rules). The prices are AA's B2B price for this agency; the page and
// the API serve them to the owner and the agent only.
import type { ColumnDef } from "@tanstack/react-table";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, DataTable, K, Select } from "@/app/_kit";
import { AA_TARIFF_PATH, discountText, paxBand, TARIFF_CURRENCIES, tariffFilename, tariffPath, type TariffItem } from "@/lib/aaTariff";
import { bffDownload } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, plural, t, type Lang, type MessageKey } from "@/lib/i18n";
import { formatAmount } from "@/lib/proposals";

/** The seven columns' width: on a desktop narrower than this the table scrolls inside its own frame (never the page);
 * below 768 px the kit shows a card per row. */
export const TARIFF_TABLE_MIN_WIDTH = 680;

const small: React.CSSProperties = { display: "block", fontSize: 12, color: K.muted, overflowWrap: "anywhere" };

export function AaTariffToolbar({ currency, lang }: { currency: string; lang: Lang }) {
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      const res = await bffDownload(tariffPath(currency, "csv"), { filename: tariffFilename(currency) });
      if (!res.ok && !res.redirecting) setError(tr("tariff.downloadError"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="tariff-toolbar" data-print="hide" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
      <Select
        size="sm"
        aria-label={tr("tariff.currency")}
        value={currency}
        data-testid="tariff-currency"
        onChange={(e) => router.push(`${AA_TARIFF_PATH}?currency=${encodeURIComponent(e.target.value)}`)}
        options={TARIFF_CURRENCIES.map((c) => ({ value: c, label: c }))}
      />
      <Button type="button" size="sm" variant="secondary" disabled={busy} onClick={download} testId="tariff-download">
        {busy ? tr("tariff.downloading") : tr("tariff.download")}
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => window.print()} testId="tariff-print">
        {tr("tariff.print")}
      </Button>
      {error && (
        <span role="alert" style={{ color: K.danger, fontSize: 12.5 }}>
          {error}
        </span>
      )}
    </div>
  );
}

export function AaTariffTable({ items, lang, locale }: { items: TariffItem[]; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const money = (m: { amount: string; currency: string } | null | undefined) => (m ? formatAmount(m.amount, m.currency, locale) : "—");
  const columns: ColumnDef<TariffItem, unknown>[] = [
    {
      id: "tour",
      header: tr("tariff.col.tour"),
      // Title then country: what the search box matches and the order of a sort by tour.
      accessorFn: (i) => [i.title ?? i.source_id, i.country].filter(Boolean).join(" "),
      cell: ({ row }) => (
        <span data-testid="tariff-row" style={{ display: "block", minWidth: 0, overflowWrap: "anywhere" }}>
          <strong>{row.original.title ?? row.original.source_id}</strong>
          {(row.original.country || row.original.duration_days) && (
            <span style={small}>
              {[row.original.country, row.original.duration_days ? plural(lang, "aa.add.days", row.original.duration_days) : null]
                .filter(Boolean)
                .join(" · ")}
            </span>
          )}
        </span>
      ),
    },
    { id: "season", header: tr("tariff.col.season"), accessorFn: (i) => i.season ?? "", cell: ({ row }) => row.original.season ?? "—" },
    {
      id: "dates",
      header: tr("tariff.col.dates"),
      accessorFn: (i) => i.valid_from,
      cell: ({ row }) => (
        <span style={{ whiteSpace: "nowrap" }}>
          {fmt(tr("tariff.dates"), { from: formatDate(row.original.valid_from, locale), to: formatDate(row.original.valid_to, locale) })}
        </span>
      ),
    },
    { id: "pax", header: tr("tariff.col.pax"), accessorFn: (i) => i.pax_min, cell: ({ row }) => paxBand(row.original, lang) },
    {
      id: "perPerson",
      header: tr("tariff.col.perPerson"),
      meta: { align: "right" },
      accessorFn: (i) => Number(i.b2b_price_per_person?.amount ?? 0),
      cell: ({ row }) => <strong data-testid="tariff-price">{money(row.original.b2b_price_per_person)}</strong>,
    },
    {
      id: "discount",
      header: tr("tariff.col.discount"),
      meta: { align: "right" },
      accessorFn: (i) => Number(i.agency_discount_pct),
      cell: ({ row }) => discountText(row.original.agency_discount_pct),
    },
    {
      id: "rrp",
      header: tr("tariff.col.rrp"),
      meta: { align: "right" },
      enableSorting: false,
      cell: ({ row }) => money(row.original.rrp_per_person),
    },
  ];
  return (
    <div data-testid="tariff-table" data-print="sheet" style={{ minWidth: 0 }}>
      <DataTable<TariffItem>
        data={items}
        columns={columns}
        tableId="crm-aa-tariff"
        getRowId={(i) => `${i.source_id}:${i.valid_from}:${i.pax_min}`}
        searchPlaceholder={tr("tariff.search")}
        paginate={false}
        compact
        tableMinWidth={TARIFF_TABLE_MIN_WIDTH}
        emptyTitle={tr("tariff.empty.title")}
        emptyDescription={tr("tariff.empty.body")}
      />
    </div>
  );
}
