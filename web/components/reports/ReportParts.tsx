// web/components/reports/ReportParts.tsx — the building blocks of A-14 Reports and the A-01 Home tiles (AA-966): the
// KPI tile row (kit KpiTile, each tile opens its work list), a horizontal bar list (one value per row, the bar in the
// accent colour and the number always written next to it, so nothing is read from colour alone). Server components; no
// data fetching here. Tables are components/reports/ReportTable.tsx (the kit DataTable, a client component).
import { K, KpiTile } from "@/app/_kit";
import { type Lang, t, type MessageKey } from "@/lib/i18n";
import { barWidths, type Compare, type Tile, tileHint, tileValue } from "@/lib/reports";

export function TileRow({
  tiles,
  compare,
  currency,
  lang,
  locale,
  testId,
}: {
  tiles: Tile[];
  compare: Compare;
  currency: string;
  lang: Lang;
  locale: string;
  testId?: string;
}) {
  return (
    <div
      data-testid={testId}
      style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", minWidth: 0 }}
    >
      {tiles.map((tile) => {
        const hint = tileHint(tile, compare, lang, locale, currency);
        const basis = tile.key === "aa_share" && tile.basis ? t(lang, `reports.basis.${tile.basis}` as MessageKey) : null;
        return (
          <a
            key={tile.key}
            href={tile.href}
            data-testid={`reports-tile-${tile.key}`}
            style={{ textDecoration: "none", color: "inherit", minWidth: 0 }}
          >
            <KpiTile
              size="sm"
              label={t(lang, `reports.tile.${tile.key}` as MessageKey)}
              value={tileValue(tile, currency, locale)}
              hint={basis ? `${hint.text} · ${basis}` : hint.text}
              hintTone={hint.tone}
              info={t(lang, `reports.tile.${tile.key}.info` as MessageKey)}
            />
          </a>
        );
      })}
    </div>
  );
}

export function BarList({
  rows,
  empty,
  testId,
}: {
  rows: { key: string; label: string; value: number; text: string }[];
  empty: string;
  testId?: string;
}) {
  if (!rows.length) return <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{empty}</p>;
  const widths = barWidths(rows.map((r) => r.value));
  return (
    <ul data-testid={testId} style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
      {rows.map((r, i) => (
        <li key={r.key} style={{ display: "grid", gap: 3, minWidth: 0 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 13 }}>
            <span style={{ overflowWrap: "anywhere", minWidth: 0 }}>{r.label}</span>
            <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap", color: K.ink }}>{r.text}</span>
          </div>
          <div aria-hidden style={{ height: 6, borderRadius: 3, background: K.line, overflow: "hidden" }}>
            <div style={{ width: `${widths[i]}%`, height: "100%", borderRadius: 3, background: K.accent }} />
          </div>
        </li>
      ))}
    </ul>
  );
}
