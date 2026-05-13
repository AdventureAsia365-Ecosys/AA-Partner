"use client";
// app/(tenant)/portal/_components/PoolTab.tsx
// API: GET /api/tenant/v1/tours/pool?page=N&page_size=20&search=X&country=Y
//      POST /api/tenant/v1/tours/pool/{id}/rewrite

import { useState, useEffect, useCallback } from "react";
import { Search, ChevronRight, X, RotateCcw, Globe2 } from "lucide-react";
import {
  T, serif, mono, sans,
  Card, ScoreBadge, Badge, Btn, LoadingScreen, EmptyState,
  parseHighlights, fmtDate, statusVariant,
} from "./ui";
import type { Tab } from "./Sidebar";
import { PoolFilters, type PoolFiltersState } from "./PoolFilters";

interface PoolTour {
  id: string; tour_id: string; aa_name: string; aa_subtitle: string;
  aa_summary: string; aa_highlights: string; aa_itineraries: string | null;
  seo_title: string; seo_meta: string; seo_keywords_used: string;
  quality_score: number; published_at: string;
  country: string | null; duration: string | null; price_raw: string | null;
  already_rewritten: boolean;
}

const PAGE_SIZE = 20;

export default function PoolTab({ onRewriteDone, externalSearch = "" }: { onRewriteDone: () => void; externalSearch?: string }) {
  const [tours, setTours]     = useState<PoolTour[]>([]);
  const [total, setTotal]     = useState(0);
  const [countries, setCountries] = useState<string[]>([]);
  const [page, setPage]       = useState(1);
  const [search, setSearch]   = useState("");

  // Sync external search (from topbar)
  useEffect(() => { if (externalSearch !== undefined) { setSearch(externalSearch); setPage(1); } }, [externalSearch]);
  const [country, setCountry] = useState("");
  const [poolFilters, setPoolFilters] = useState<PoolFiltersState>({ duration: "all", sort: "newest" });
  const [inCatalogSet, setInCatalogSet] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<PoolTour | null>(null);
  const [checked, setChecked]   = useState<Set<string>>(new Set());
  const [panelTab, setPanelTab] = useState<"details" | "rewrite">("details");
  const [expandItin, setExpandItin] = useState(false);

  // Rewrite config
  const [rwLang, setRwLang]   = useState("en-US");
  const [rwSeo, setRwSeo]     = useState("standard");
  const [rwBrand, setRwBrand] = useState(true);
  const [rewrit, setRewrit]   = useState(false);

  const fetchPool = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
      if (search)  params.set("search", search);
      if (country) params.set("country", country);
      params.set("sort", poolFilters.sort);
      if (poolFilters.duration !== "all") {
        if (poolFilters.duration === "8+") {
          params.set("duration_min", "8");
        } else {
          const [min, max] = poolFilters.duration.split("-");
          params.set("duration_min", min);
          params.set("duration_max", max);
        }
      }

      const [poolRes, versionsRes] = await Promise.allSettled([
        fetch(`/api/tenant/v1/tours/pool?${params}`),
        fetch("/api/tenant/v1/tours/my-versions?page_size=200"),
      ]);

      if (poolRes.status === "fulfilled" && poolRes.value.ok) {
        const d = await poolRes.value.json();
        setTours(d.data ?? []);
        setTotal(d.pagination?.total ?? 0);
        if (d.countries?.length) setCountries(d.countries);
      }

      if (versionsRes.status === "fulfilled" && versionsRes.value.ok) {
        const vd = await versionsRes.value.json();
        const versions: { published_tour_id?: string; status: string }[] = vd.data ?? [];
        setInCatalogSet(new Set(
          versions
            .filter(v => v.status === "approved" && v.published_tour_id)
            .map(v => v.published_tour_id as string)
        ));
      }
    } finally { setLoading(false); }
  }, [page, search, country, poolFilters]);

  useEffect(() => { fetchPool(); }, [fetchPool]);

  async function doRewrite(ids: string[]) {
    setRewrit(true);
    try {
      for (const id of ids) {
        await fetch(`/api/tenant/v1/tours/pool/${id}/rewrite`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rewrite_language: rwLang, seo_mode: rwSeo, use_brand_rules: rwBrand }),
        });
      }
      setSelected(null);
      setChecked(new Set());
      onRewriteDone();
    } finally { setRewrit(false); }
  }

  const rewriteTargets = checked.size > 0 ? Array.from(checked) : selected ? [selected.id] : [];

  return (
    <div style={{ display: "grid", gridTemplateColumns: selected ? "minmax(0,36%) minmax(0,64%)" : "1fr", gap: 20, alignItems: "start" }}>

      {/* LEFT — list */}
      <div>
        {/* Filter bar */}
        <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
          <div style={{ position: "relative", flex: 1 }}>
            <Search size={13} style={{ position: "absolute", left: 11, top: "50%", transform: "translateY(-50%)", color: T.muted2 }} />
            <input value={search}
              onChange={e => { setSearch(e.target.value); setPage(1); }}
              placeholder="Search tours by name…"
              style={{ width: "100%", padding: "9px 12px 9px 32px", background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, outline: "none", fontFamily: sans, boxSizing: "border-box" }} />
          </div>
          <select value={country} onChange={e => { setCountry(e.target.value); setPage(1); }}
            style={{ padding: "9px 12px", background: T.card, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans, cursor: "pointer" }}>
            <option value="">All Countries</option>
            {countries.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <PoolFilters
            filters={poolFilters}
            onChange={update => { setPoolFilters(prev => ({ ...prev, ...update })); setPage(1); }}
          />
        </div>

        {/* Batch bar */}
        {checked.size > 0 && (
          <div style={{ marginBottom: 12, padding: "10px 16px", background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 8, display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13, color: T.amber, fontWeight: 600 }}>{checked.size} tour{checked.size > 1 ? "s" : ""} selected</span>
            <Btn variant="primary" size="sm" disabled={rewrit} onClick={() => doRewrite(Array.from(checked))}>
              {rewrit ? "Rewriting…" : `Rewrite ${checked.size}`}
            </Btn>
            <Btn variant="ghost" size="sm" onClick={() => setChecked(new Set())}>Clear</Btn>
          </div>
        )}

        {/* Stats */}
        {!loading && (
          <div style={{ fontSize: 12, color: T.muted2, marginBottom: 12 }}>
            {total.toLocaleString()} tours available
          </div>
        )}

        {/* Tour list */}
        {loading ? <LoadingScreen message="Loading pool…" /> : tours.length === 0 ? (
          <EmptyState icon="🌏" title="No tours found" sub="Try adjusting your search or filters" />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {tours.map(t => {
              const isActive  = selected?.id === t.id;
              const isChecked = checked.has(t.id);
              return (
                <TourRow key={t.id} tour={t} isActive={isActive} isChecked={isChecked}
                  inCatalogSet={inCatalogSet}
                  onSelect={() => { setSelected(isActive ? null : t); setPanelTab("details"); setExpandItin(false); }}
                  onCheck={() => setChecked(prev => { const s = new Set(prev); s.has(t.id) ? s.delete(t.id) : s.add(t.id); return s; })}
                />
              );
            })}
          </div>
        )}

        {/* Pagination */}
        {total > PAGE_SIZE && (
          <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 16 }}>
            <Btn variant="secondary" size="sm" disabled={page === 1} onClick={() => setPage(p => p - 1)}>← Prev</Btn>
            <span style={{ padding: "5px 14px", fontSize: 12, color: T.muted, alignSelf: "center" }}>
              {page} / {Math.ceil(total / PAGE_SIZE)}
            </span>
            <Btn variant="secondary" size="sm" disabled={page * PAGE_SIZE >= total} onClick={() => setPage(p => p + 1)}>Next →</Btn>
          </div>
        )}
      </div>

      {/* RIGHT — detail panel */}
      {selected && (
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, overflow: "hidden", position: "sticky", top: 20 }}>
          {/* Header */}
          <div style={{ borderBottom: `1px solid ${T.line}` }}>
            <div style={{ padding: "16px 20px 12px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 15, fontWeight: 700, color: T.ink, marginBottom: 4, lineHeight: 1.3 }}>{selected.aa_name}</div>
                  <div style={{ fontSize: 12, color: T.muted, display: "flex", gap: 10, flexWrap: "wrap" }}>
                    {selected.country && <span>📍 {selected.country}</span>}
                    {selected.duration && <span>⏱ {selected.duration}</span>}
                    {selected.price_raw && <span>💰 {selected.price_raw}</span>}
                  </div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <ScoreBadge score={selected.quality_score} />
                  <button onClick={() => setSelected(null)} style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 2 }}>
                    <X size={16} />
                  </button>
                </div>
              </div>
              {inCatalogSet.has(selected.id) ? (
                <span style={{ marginTop: 8, display: "inline-block", fontSize: 11, padding: "2px 8px", background: T.greenSoft, color: T.green, borderRadius: 20, fontWeight: 600 }}>
                  ✓ In My Catalog
                </span>
              ) : selected.already_rewritten ? (
                <span style={{ marginTop: 8, display: "inline-block", fontSize: 11, padding: "2px 8px", background: T.goldTint, color: T.amber, borderRadius: 20, fontWeight: 600 }}>
                  ↻ Version in progress
                </span>
              ) : null}
            </div>
            {/* Tabs */}
            <div style={{ display: "flex", padding: "0 20px" }}>
              {(["details", "rewrite"] as const).map(t => (
                <button key={t} onClick={() => setPanelTab(t)} style={{
                  padding: "8px 16px", fontSize: 13, fontWeight: 600, border: "none",
                  background: "none", cursor: "pointer", fontFamily: sans,
                  color: panelTab === t ? T.gold : T.muted,
                  borderBottom: `2px solid ${panelTab === t ? T.gold : "transparent"}`,
                  transition: "all .15s",
                }}>
                  {t === "details" ? "📄 Tour Details" : "✏️ Rewrite Config"}
                </button>
              ))}
            </div>
          </div>

          {/* Body */}
          <div style={{ padding: 20, maxHeight: "70vh", overflowY: "auto" }}>
            {panelTab === "details" ? (
              <DetailPanel tour={selected} expandItin={expandItin} setExpandItin={setExpandItin} />
            ) : (
              <RewritePanel
                rwLang={rwLang} setRwLang={setRwLang}
                rwSeo={rwSeo} setRwSeo={setRwSeo}
                rwBrand={rwBrand} setRwBrand={setRwBrand}
                rewrit={rewrit} targets={rewriteTargets}
                onRewrite={() => doRewrite(rewriteTargets)}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Tour row ──────────────────────────────────────────────────────────────────

function TourRow({ tour, isActive, isChecked, inCatalogSet, onSelect, onCheck }: {
  tour: PoolTour; isActive: boolean; isChecked: boolean;
  inCatalogSet: Set<string>;
  onSelect: () => void; onCheck: () => void;
}) {
  const kws = (() => { try { const v = JSON.parse(JSON.parse(tour.seo_keywords_used)); return Array.isArray(v) ? v.slice(0, 3) : []; } catch { return []; } })();
  return (
    <div style={{
      background: isActive ? "rgba(219,150,40,0.04)" : T.card,
      border: `1px solid ${isChecked ? T.gold : isActive ? "rgba(219,150,40,0.3)" : T.line}`,
      borderRadius: 10, padding: "12px 14px", cursor: "pointer",
      transition: "all .15s",
    }} onClick={onSelect}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <input type="checkbox" checked={isChecked} onChange={() => {}} onClick={e => { e.stopPropagation(); onCheck(); }}
          style={{ marginTop: 3, flexShrink: 0, accentColor: T.gold, cursor: "pointer" }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.ink }}>{tour.aa_name}</span>
            {inCatalogSet.has(tour.id) ? (
              <span style={{ fontSize: 10, padding: "1px 6px", background: T.greenSoft, color: T.green, borderRadius: 20, fontWeight: 600, flexShrink: 0 }}>✓ In My Catalog</span>
            ) : tour.already_rewritten ? (
              <span style={{ fontSize: 10, padding: "1px 6px", background: T.goldTint, color: T.amber, borderRadius: 20, fontWeight: 600, flexShrink: 0 }}>↻ In progress</span>
            ) : null}
          </div>
          {tour.aa_subtitle && (
            <div style={{ fontSize: 12, color: T.muted, lineHeight: 1.4, marginBottom: 6 }}>
              {tour.aa_subtitle.slice(0, 100)}{tour.aa_subtitle.length > 100 ? "…" : ""}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {tour.country && <Tag icon={<Globe2 size={10} />}>{tour.country}</Tag>}
            {tour.duration && <Tag>{tour.duration}</Tag>}
            {kws.map((k: string) => <Tag key={k} gold>{k}</Tag>)}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6, flexShrink: 0 }}>
          <ScoreBadge score={tour.quality_score} />
          <ChevronRight size={14} color={T.muted2} />
        </div>
      </div>
    </div>
  );
}

function Tag({ children, icon, gold = false }: { children: React.ReactNode; icon?: React.ReactNode; gold?: boolean }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 3,
      fontSize: 11, padding: "1px 8px", borderRadius: 20,
      background: gold ? "rgba(219,150,40,0.09)" : T.line2,
      color: gold ? T.amber : T.muted,
    }}>
      {icon}{children}
    </span>
  );
}

// ── Detail panel ──────────────────────────────────────────────────────────────

function DetailPanel({ tour, expandItin, setExpandItin }: {
  tour: PoolTour; expandItin: boolean; setExpandItin: (v: boolean) => void;
}) {
  const highlights = parseHighlights(tour.aa_highlights);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      {/* Summary */}
      <Section label="Summary">
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.65, margin: 0 }}>{tour.aa_summary}</p>
      </Section>
      {/* Highlights */}
      {highlights.length > 0 && (
        <Section label="Highlights">
          <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 5 }}>
            {highlights.map((h, i) => (
              <li key={i} style={{ display: "flex", gap: 8, fontSize: 12.5, color: T.body, lineHeight: 1.5 }}>
                <span style={{ color: T.gold, fontWeight: 700, flexShrink: 0 }}>•</span>{h}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {/* Itinerary */}
      {tour.aa_itineraries && (
        <Section label="Itinerary">
          <div style={{ position: "relative" }}>
            <div style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.7, whiteSpace: "pre-wrap", maxHeight: expandItin ? "none" : 120, overflow: expandItin ? "visible" : "hidden" }}>
              {tour.aa_itineraries}
            </div>
            {!expandItin && <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, height: 40, background: "linear-gradient(transparent, #fff)" }} />}
          </div>
          <button onClick={() => setExpandItin(!expandItin)}
            style={{ marginTop: 6, width: "100%", padding: "6px 0", fontSize: 12, color: T.gold, background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 6, cursor: "pointer", fontWeight: 600, fontFamily: sans }}>
            {expandItin ? "▲ Collapse" : "▼ Show full itinerary"}
          </button>
        </Section>
      )}
      {/* SEO */}
      <Section label="SEO">
        <div style={{ background: T.bg, borderRadius: 8, padding: "10px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          {tour.seo_title && <SeoLine label="Title" value={tour.seo_title} />}
          {tour.seo_meta  && <SeoLine label="Meta"  value={tour.seo_meta}  />}
        </div>
      </Section>
      <div style={{ fontSize: 11, color: T.muted2, fontFamily: mono }}>
        Published {fmtDate(tour.published_at)}
      </div>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 8 }}>{label}</div>
      {children}
    </div>
  );
}

function SeoLine({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: T.muted2, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 12, color: T.ink, fontWeight: 500, lineHeight: 1.4 }}>{value}</div>
    </div>
  );
}

// ── Rewrite panel ─────────────────────────────────────────────────────────────

function RewritePanel({ rwLang, setRwLang, rwSeo, setRwSeo, rwBrand, setRwBrand, rewrit, targets, onRewrite }: {
  rwLang: string; setRwLang: (v: string) => void;
  rwSeo: string; setRwSeo: (v: string) => void;
  rwBrand: boolean; setRwBrand: (v: boolean) => void;
  rewrit: boolean; targets: string[]; onRewrite: () => void;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      {/* Brand rules */}
      <ConfigBlock title="Brand Rules" active={rwBrand} onToggle={() => setRwBrand(!rwBrand)}>
        <div style={{ fontSize: 12, color: T.muted, lineHeight: 1.5 }}>
          {rwBrand ? "Your brand voice and forbidden words will be injected into the LLM prompt." : "Using Adventure Asia default rules (29 validated brand rules)."}
        </div>
      </ConfigBlock>

      {/* Language */}
      <div>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 8 }}>Language</div>
        <div style={{ display: "flex", gap: 8 }}>
          {["en-US", "en-GB"].map(l => (
            <button key={l} onClick={() => setRwLang(l)} style={{
              flex: 1, padding: "8px 10px", borderRadius: 7, cursor: "pointer", fontFamily: sans,
              border: `1px solid ${rwLang === l ? T.gold : T.line}`,
              background: rwLang === l ? T.goldTint : T.bg,
              color: rwLang === l ? T.amber : T.muted,
              fontSize: 12.5, fontWeight: rwLang === l ? 700 : 400,
            }}>{l}</button>
          ))}
        </div>
      </div>

      {/* SEO Mode */}
      <div>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 8 }}>SEO Mode</div>
        {[
          { v: "standard",  l: "Standard",  d: "Balanced keyword density" },
          { v: "aggressive",l: "Aggressive", d: "Maximum keywords + PAA questions" },
          { v: "minimal",   l: "Minimal",    d: "Brand voice first, light SEO" },
        ].map(s => (
          <button key={s.v} onClick={() => setRwSeo(s.v)} style={{
            width: "100%", padding: "9px 12px", marginBottom: 6,
            borderRadius: 7, cursor: "pointer", textAlign: "left", fontFamily: sans,
            border: `1px solid ${rwSeo === s.v ? T.gold : T.line}`,
            background: rwSeo === s.v ? T.goldTint : T.bg,
            color: rwSeo === s.v ? T.amber : T.muted,
          }}>
            <div style={{ fontSize: 12.5, fontWeight: 600 }}>{s.l}</div>
            <div style={{ fontSize: 11, opacity: 0.75, marginTop: 1 }}>{s.d}</div>
          </button>
        ))}
      </div>

      {/* Cost estimate */}
      <div style={{ background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "10px 14px", fontSize: 12, color: T.muted, fontFamily: mono }}>
        Est. ~$0.018/tour · Bedrock claude-sonnet-4-5 · ~4,200 tokens
      </div>

      <Btn variant="primary" size="lg" disabled={rewrit || targets.length === 0} onClick={onRewrite} style={{ width: "100%" }}>
        {rewrit ? "Starting rewrite…" : targets.length > 1 ? `Rewrite ${targets.length} tours` : "Rewrite this tour"}
      </Btn>
      <div style={{ fontSize: 11, color: T.muted2, textAlign: "center" }}>
        Results appear in <strong>My Catalog</strong> within ~30 seconds.
      </div>
    </div>
  );
}

function ConfigBlock({ title, active, onToggle, children }: {
  title: string; active: boolean; onToggle: () => void; children: React.ReactNode;
}) {
  return (
    <div style={{ padding: "12px 14px", background: active ? T.goldTint : T.bg, border: `1px solid ${active ? T.goldSoft : T.line}`, borderRadius: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: active ? 8 : 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: active ? T.amber : T.muted }}>{title}</div>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input type="checkbox" checked={active} onChange={onToggle} style={{ accentColor: T.gold }} />
          <span style={{ fontSize: 12, color: T.muted }}>{active ? "On" : "Off"}</span>
        </label>
      </div>
      {active && children}
    </div>
  );
}
