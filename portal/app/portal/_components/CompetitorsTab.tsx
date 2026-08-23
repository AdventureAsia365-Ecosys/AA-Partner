"use client";
// app/(tenant)/portal/_components/CompetitorsTab.tsx — AA-445-02.
//
// Tenant-facing UI for acp_silver_s2.competitor_inputs (AA-88, already built — this is a new
// UI on top of an existing, previously-unused-by-any-frontend API, not a new backend). Feeds
// B4/score_distinctiveness() (services/acp_shared/competitor_index.py): a tenant's declared
// competitor domains here are what T5 atomize fetches to score each new atom's
// `distinctiveness` against (see docs/claude_audit/AA-445-01-dfs-distinctiveness-step0-audit.md
// Q3/Q4 and docs/implementation-notes/AA-445-02-distinctiveness-dfs-t2-build.md).
//
// API: GET/POST/PATCH/DELETE /api/tenant/v1/competitors — reaches the existing
// /v1/competitors router (api/routers/v1_competitors.py) unchanged, through the generic
// /api/tenant/[...path] proxy (no new backend route needed — that proxy forwards
// /api/tenant/v1/competitors -> {API_URL}/v1/competitors, already Bearer/X-Admin-Secret-aware).
//
// Grain is (tenant_id, country) with a max of 10 ACTIVE urls per country (API-enforced,
// MAX_PER_COUNTRY in v1_competitors.py) — grouped by country in this UI to match.

import { useState, useEffect } from "react";
import { Plus, Trash2, Swords } from "lucide-react";
import {
  T, serif, sans, mono,
  Card, CardHead, Btn, EmptyState, LoadingScreen,
} from "./ui";

interface Competitor {
  id: string; country: string; url: string; label: string | null;
  is_active: boolean; created_at: string;
}

interface CompetitorsResponse {
  data: Competitor[];
  active_count_by_country: Record<string, number>;
  max_per_country: number;
}

export default function CompetitorsTab() {
  const [resp, setResp]       = useState<CompetitorsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [country, setCountry] = useState("");
  const [url, setUrl]         = useState("");
  const [label, setLabel]     = useState("");
  const [saving, setSaving]   = useState(false);
  const [error, setError]     = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/tenant/v1/competitors");
      if (r.ok) setResp(await r.json());
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  async function addCompetitor() {
    setError(null);
    if (!country.trim() || !url.trim()) {
      setError("Country and URL are required.");
      return;
    }
    setSaving(true);
    try {
      const r = await fetch("/api/tenant/v1/competitors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ country: country.trim(), url: url.trim(), label: label.trim() || null }),
      });
      if (r.ok) {
        setUrl(""); setLabel("");
        await load();
      } else {
        const d = await r.json().catch(() => ({}));
        setError(d.detail || `Failed to add (HTTP ${r.status})`);
      }
    } finally { setSaving(false); }
  }

  async function removeCompetitor(id: string) {
    const r = await fetch(`/api/tenant/v1/competitors/${id}`, { method: "DELETE" });
    if (r.ok) await load();
  }

  if (loading) return <LoadingScreen message="Loading competitors…" />;

  const data = resp?.data ?? [];
  const activeByCountry = resp?.active_count_by_country ?? {};
  const maxPerCountry = resp?.max_per_country ?? 10;
  const grouped: Record<string, Competitor[]> = {};
  for (const c of data) {
    (grouped[c.country] ||= []).push(c);
  }
  const countries = Object.keys(grouped).sort();
  const atLimit = country.trim() ? (activeByCountry[country.trim()] ?? 0) >= maxPerCountry : false;

  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>Competitors</h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Declare direct competitors per market. Used to score how distinctive each new atom is
          against what competitors already say — up to {maxPerCountry} active URLs per country.
        </p>
      </div>

      {/* Add form */}
      <Card style={{ marginBottom: 20 }}>
        <CardHead title="Add a competitor" />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr 1.4fr auto", gap: 10, alignItems: "start" }}>
          <div>
            <input value={country} onChange={e => setCountry(e.target.value)}
              placeholder="Country (e.g. Vietnam)"
              style={inputStyle} />
          </div>
          <div>
            <input value={url} onChange={e => setUrl(e.target.value)}
              placeholder="https://competitor.com"
              style={inputStyle} />
          </div>
          <div>
            <input value={label} onChange={e => setLabel(e.target.value)}
              placeholder="Label (optional)"
              style={inputStyle} />
          </div>
          <Btn variant="primary" onClick={addCompetitor} disabled={saving || atLimit}>
            <Plus size={13} /> Add
          </Btn>
        </div>
        {atLimit && (
          <div style={{ fontSize: 11.5, color: T.amber, marginTop: 8 }}>
            {country.trim()} is already at the {maxPerCountry}-URL limit — remove one before adding another.
          </div>
        )}
        {error && <div style={{ fontSize: 11.5, color: T.red, marginTop: 8 }}>{error}</div>}
      </Card>

      {/* Grouped list */}
      {countries.length === 0 ? (
        <EmptyState icon={<Swords />} title="No competitors declared yet"
          sub="Add at least one competitor domain per market so new atoms can be scored for distinctiveness instead of defaulting to a neutral MED." />
      ) : (
        countries.map(c => (
          <Card key={c} style={{ marginBottom: 14 }}>
            <CardHead
              title={c}
              action={<span style={{ fontFamily: mono }}>{activeByCountry[c] ?? 0} / {maxPerCountry} active</span>}
            />
            {grouped[c].map(comp => (
              <div key={comp.id} style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "9px 0", borderTop: `1px solid ${T.line2}`,
                opacity: comp.is_active ? 1 : 0.45,
              }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13, color: T.ink, fontFamily: sans, fontWeight: 500,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {comp.url}
                  </div>
                  {comp.label && <div style={{ fontSize: 11.5, color: T.muted, marginTop: 2 }}>{comp.label}</div>}
                  {!comp.is_active && <div style={{ fontSize: 11, color: T.muted2, marginTop: 2 }}>Removed</div>}
                </div>
                {comp.is_active && (
                  <button onClick={() => removeCompetitor(comp.id)}
                    title="Remove"
                    style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 6, flexShrink: 0 }}>
                    <Trash2 size={14} />
                  </button>
                )}
              </div>
            ))}
          </Card>
        ))
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`,
  borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans, outline: "none", boxSizing: "border-box",
};
