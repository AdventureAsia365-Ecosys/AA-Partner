"use client";
// app/(tenant)/portal/t0-brand/page.tsx — AA-430 route migration.
// Was the "brand" tab in the old portal/page.tsx (T0 — Brand Identity Setup).
//
// AA-445-02: added a "Competitors" sub-tab alongside Brand Identity. Per ADR-2026-038 §0.4
// ("Intake đối thủ → đặt ở T0 (Brand Setup), cùng chỗ tenant nhập thông tin riêng của họ") —
// competitor declaration is the same class of one-time tenant setup as brand identity, so it
// lives at the same T0 route rather than a new top-level portal page. Both tabs stay mounted
// independently (each owns its own fetch/state) — this file only toggles which is visible.
import { useState } from "react";
import { T, sans } from "../_components/ui";
import BrandTab from "../_components/BrandTab";
import CompetitorsTab from "../_components/CompetitorsTab";

type SubTab = "brand" | "competitors";

export default function T0BrandPage() {
  const [tab, setTab] = useState<SubTab>("brand");

  return (
    <div>
      <div style={{ display: "flex", gap: 4, marginBottom: 24, borderBottom: `1px solid ${T.line}` }}>
        {([
          { key: "brand", label: "Brand Identity" },
          { key: "competitors", label: "Competitors" },
        ] as { key: SubTab; label: string }[]).map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            style={{
              padding: "10px 16px", background: "none", border: "none", cursor: "pointer",
              fontFamily: sans, fontSize: 13, fontWeight: 600,
              color: tab === t.key ? T.ink : T.muted,
              borderBottom: `2px solid ${tab === t.key ? T.gold : "transparent"}`,
              marginBottom: -1,
            }}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === "brand" ? <BrandTab /> : <CompetitorsTab />}
    </div>
  );
}
