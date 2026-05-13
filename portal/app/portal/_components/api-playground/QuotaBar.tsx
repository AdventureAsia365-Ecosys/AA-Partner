"use client";
import { T, sans } from "../ui";

interface QuotaBarProps {
  quota?: Record<string, unknown> | null;
}

export function QuotaBar({ quota }: QuotaBarProps) {
  const rewrites   = quota?.rewrites_remaining as number | undefined;
  const rewritesOf = quota?.rewrites_limit    as number | undefined;
  const calls      = quota?.api_calls_month   as number | undefined;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <QuotaRow
        label="Rewrites left"
        value={rewrites != null ? String(rewrites) : "—"}
        total={rewritesOf}
        warn={rewrites != null && rewrites < 5}
      />
      {calls != null && (
        <QuotaRow label="API calls" value={String(calls)} />
      )}
    </div>
  );
}

function QuotaRow({ label, value, total, warn = false }: {
  label: string; value: string; total?: number; warn?: boolean;
}) {
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: T.muted, marginBottom: 3, fontFamily: sans }}>
        <span>{label}</span>
        <span style={{ color: warn ? T.red : T.ink, fontWeight: 600 }}>
          {value}{total != null ? ` / ${total}` : ""}
        </span>
      </div>
      {total != null && (
        <div style={{ height: 4, background: T.line2, borderRadius: 999, overflow: "hidden" }}>
          <div style={{
            height: "100%", borderRadius: 999,
            width: `${Math.min(100, (Number(value) / total) * 100)}%`,
            background: warn ? T.red : T.gold,
            transition: "width 0.4s ease",
          }} />
        </div>
      )}
    </div>
  );
}
