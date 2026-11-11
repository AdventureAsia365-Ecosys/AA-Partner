"use client";
// web/app/(studio)/portal/_components/BrandTab.tsx
// API: GET  /api/tenant/admin/brand-identity          (AA-424: was /api/admin/brand-identity,
//      POST /api/tenant/admin/brand-identity           which requireAdmin()-gated and 401'd every
//                                                       real tenant session — see AA-423/424.
//                                                       Routed through the tenant proxy so
//                                                       cis_tenant_token reaches the now
//                                                       tenant-JWT-aware backend endpoint.)
//      POST /api/tenant/admin/brand-identity/upload    (AA-441: was /api/admin/brand-identity/
//                                                        upload — same 401 class as above, PLUS
//                                                        the backend itself hardcoded AA-internal
//                                                        tenant_id and expected a JSON
//                                                        {filename,content_type} body → presigned
//                                                        S3 URL, never actually receiving the
//                                                        file this component always sent as
//                                                        multipart. Backend now accepts the
//                                                        multipart file directly and resolves
//                                                        tenant_id from the caller; the tenant
//                                                        proxy now passes multipart through
//                                                        unmangled instead of forcing JSON.)
//
// AA-760 — the brand record is a react-query query (was a fetch in an effect that also filled the
// form: the last portal lint error). `BrandTab` owns the query; `BrandForm` takes the record and
// initialises its fields from it once (useState initialisers, no effect). The form remounts (new
// `key`) only when it must start again from the server: after a save and on "Back to active". A
// failed load shows an error with Retry instead of an empty form that could be saved over the
// real one. Tenant copy: no "29 core brand standards", "rewrite", "quality thresholds" or
// "system prompt" (tenant sees content only).

import { useState, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, History, Upload, Check, RotateCcw } from "lucide-react";
import {
  Button, EmptyState, Field, IconButton, Input, LinkButton, LoadingScreen, Textarea, apiGet, K, mono, sans, serif, useLang, useT,
} from "@/app/_kit";
import { BRAND_MESSAGES } from "./brand.messages";
import { fmtDate } from "@/lib/studio/format";
import { CtaReviewNotice, ctaWarningFrom, useCtaReviewCheck, type CtaWarning } from "./CtaReviewNotice";
import { draftedFields, marketNoteLines, wordList, type MarketSummary } from "@/lib/studio/brandMarkets";

interface BrandData {
  configured: boolean; system_prompt: string; style_guide: string;
  forbidden_words: string[] | string; version: number; updated_at: string;
  history: { version: number; is_active: boolean; system_prompt: string; style_guide: string; forbidden_words: string[]; updated_at: string }[];
  // AA-557 J.23 — same full field set Admin's own /admin/brand page has, mirrored here so
  // Admin's Tenant→Brand tab (J.24) and this page read/write the SAME shared.tenant_brand_rules
  // row, not 2 separate schemas that need syncing.
  brand_name: string; brand_type: string; core_idea: string;
  customer_segment: string; customer_mindset: string;
  tone_of_voice: string[]; good_examples: string; target_markets: string[];
  default_cta?: string;
  // AA-1080 — the brand's own words (AA-800) and the market note (unsupported markets with the reason).
  signature_words?: string[];
  markets?: MarketSummary | null;
}

// AA-1008 (S6f): the brand form's fields are the kit Field + Input / Textarea (the studio's own textarea Field of the
// retired ui.tsx is gone). A long answer grows with its text (CSS `field-sizing: content`, and enough `rows` where that
// is not supported), so a saved answer is never cut mid-line; `rows` stays the minimum (capped at 16).
const FIELD_GAP = { marginBottom: 16 } as const;

/** Rows a text needs at ~44 characters a line (a phone-width textarea), plus its own line breaks. */
function rowsFor(text: string): number {
  return text.split("\n").reduce((n, line) => n + Math.max(1, Math.ceil(line.length / 44)), 0);
}

function AreaField({ label, value, onChange, rows = 3, placeholder, hint }: {
  label: string; value: string; onChange: (v: string) => void; rows?: number; placeholder?: string; hint?: React.ReactNode;
}) {
  return (
    <Field label={label} hint={hint} style={FIELD_GAP}>
      <Textarea value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        rows={Math.min(16, Math.max(rows, rowsFor(value)))}
        style={{ fieldSizing: "content", minHeight: `calc(${rows} * 1.5em + 18px)`, maxHeight: "60vh" } as React.CSSProperties} />
    </Field>
  );
}

function TextField({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <Field label={label} style={FIELD_GAP}>
      <Input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder} />
    </Field>
  );
}

// Comma-separated multi-value field (same convention the Forbidden Words field below uses) — for Target Markets /
// Tone of Voice.
function TagListField({ label, value, onChange, placeholder, hint }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; hint?: string;
}) {
  const t = useT(BRAND_MESSAGES);
  const tags = value.split(",").map(w => w.trim()).filter(Boolean);
  return (
    <Field label={label} hint={hint ?? t("commaSeparated")} style={FIELD_GAP}>
      <Input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder} />
      {tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {tags.map(tag => (
            <span key={tag} style={{ fontSize: 11, padding: "2px 8px", borderRadius: 20, background: K.accentTint, color: K.amber }}>{tag}</span>
          ))}
        </div>
      )}
    </Field>
  );
}

// AA-1080 — the saved markets: which market the tour-page keywords use, and each declared market without search
// data with the reason and the market used instead (was dropped without a word).
function MarketNoteBox({ summary }: { summary: MarketSummary | null }) {
  const t = useT(BRAND_MESSAGES);
  const lines = marketNoteLines(summary);
  if (!lines) return null;
  return (
    <div role="note" aria-label={t("marketNoteTitle")} style={{ marginTop: -6, marginBottom: 16, padding: "10px 12px",
      background: lines.unsupported.length ? K.amberSoft : K.bg, border: `1px solid ${lines.unsupported.length ? K.accentSoft : K.line}`,
      borderRadius: 8 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: K.ink, marginBottom: 4 }}>{t("marketNoteTitle")}</div>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.body, lineHeight: 1.6 }}>
        <li>{lines.defaulted ? t("marketNoteDefault") : t("marketNoteSeo", lines.seoMarket)}</li>
        {lines.unsupported.map(u => <li key={u.country}>{t("marketNoteReason", u.country, u.uses)}</li>)}
        {lines.unknown.length > 0 && <li>{t("notMarket", lines.unknown.join(", "))}</li>}
      </ul>
    </div>
  );
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

const BRAND_KEY = ["tenant-brand-identity"] as const;

type MarketNote = MarketSummary;
/** What the last save returned (kept by BrandTab: the form remounts after a save). */
interface SaveResult { marketNote: MarketNote | null; ctaWarning: CtaWarning }

function forbiddenList(raw: BrandData["forbidden_words"]): string[] {
  if (Array.isArray(raw)) return raw;
  try { return typeof raw === "string" ? JSON.parse(raw || "[]") : []; } catch { return []; }
}

export default function BrandTab() {
  const t = useT(BRAND_MESSAGES);
  const q = useQuery({
    queryKey: BRAND_KEY,
    queryFn: () => apiGet<BrandData>("/api/tenant/admin/brand-identity"),
    // The form is filled from this once; a background refetch must not race an edit.
    staleTime: Infinity,
  });
  const [formKey, setFormKey] = useState(0);
  const [result, setResult] = useState<SaveResult | null>(null);

  if (q.isPending) return <LoadingScreen message={t("loading")} />;
  if (q.isError) {
    return (
      <EmptyState icon={<AlertTriangle size={32} strokeWidth={1.5} color={K.accent} />} title={t("loadFailed")}
        description={t("loadFailedSub")}
        action={<Button variant="secondary" onClick={() => { void q.refetch(); }}>{t("tryAgain")}</Button>} />
    );
  }

  return (
    <BrandForm
      key={formKey}
      data={q.data}
      result={result}
      clearResult={() => setResult(null)}
      onSaved={async (r) => { await q.refetch(); setResult(r); setFormKey(k => k + 1); }}
      onReset={() => setFormKey(k => k + 1)}
    />
  );
}

function BrandForm({ data, result, clearResult, onSaved, onReset }: {
  data: BrandData;
  result: SaveResult | null;
  clearResult: () => void;
  onSaved: (r: SaveResult) => Promise<void>;
  /** Start again from the saved record (drops unsaved edits). */
  onReset: () => void;
}) {
  const t = useT(BRAND_MESSAGES);
  const lang = useLang();
  const [saving, setSaving]   = useState(false);
  const [dirty, setDirty]     = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [viewingV, setViewingV] = useState<number | null>(null);
  const [uploadStatus, setUploadStatus] = useState<"idle" | "uploading" | "done" | "stored" | "error">("idle");
  // AA-741 (S229): what the brief upload found.
  const [briefNotes, setBriefNotes] = useState<string[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const saved = result !== null && !dirty;
  const marketNote = result?.marketNote ?? null;

  // Form state, initialised once from the saved record.
  const [sp, setSp]   = useState(data.system_prompt ?? "");
  const [sg, setSg]   = useState(data.style_guide ?? "");
  const [fw, setFw]   = useState(() => forbiddenList(data.forbidden_words).join(", "));  // comma-separated
  // AA-557 J.23 — the extended field set.
  const [brandName, setBrandName] = useState(data.brand_name ?? "");
  const [brandType, setBrandType] = useState(data.brand_type ?? "");
  const [coreIdea, setCoreIdea] = useState(data.core_idea ?? "");
  const [targetMarkets, setTargetMarkets] = useState((data.target_markets ?? []).join(", "));
  const [customerSegment, setCustomerSegment] = useState(data.customer_segment ?? "");
  const [customerMindset, setCustomerMindset] = useState(data.customer_mindset ?? "");
  const [toneOfVoice, setToneOfVoice] = useState((data.tone_of_voice ?? []).join(", "));
  const [goodExamples, setGoodExamples] = useState(data.good_examples ?? "");
  const [defaultCta, setDefaultCta] = useState(data.default_cta ?? "");
  const [sigWords, setSigWords] = useState((data.signature_words ?? []).join(", "));
  // AA-819 follow-up — a CTA that mentions reviews or ratings is saved as typed, with a warning:
  // live while typing, and from the save response.
  const liveCtaWarning = useCtaReviewCheck(defaultCta);
  const savedCtaWarning = result?.ctaWarning ?? null;

  function markDirty() { setDirty(true); if (result) clearResult(); }

  async function save() {
    setSaving(true); setSaveError(null);
    try {
      const r = await fetch("/api/tenant/admin/brand-identity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_prompt: sp,
          style_guide: sg,
          forbidden_words: fw.split(",").map(w => w.trim()).filter(Boolean),
          brand_name: brandName,
          brand_type: brandType,
          core_idea: coreIdea,
          target_markets: targetMarkets.split(",").map(w => w.trim()).filter(Boolean),
          customer_segment: customerSegment,
          customer_mindset: customerMindset,
          tone_of_voice: toneOfVoice.split(",").map(w => w.trim()).filter(Boolean),
          good_examples: goodExamples,
          default_cta: defaultCta,
          signature_words: wordList(sigWords),
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (r.ok) {
        // The form remounts on the saved record (BrandTab bumps its key after the refetch).
        await onSaved({ marketNote: body.markets ?? null, ctaWarning: ctaWarningFrom(body) });
        return;
      } else {
        // AA-741 (S229): was silently ignored — the tenant saw nothing when a save failed.
        setSaveError(typeof body.detail === "string" ? body.detail : t("saveFailed"));
      }
    } catch {
      setSaveError(t("saveOffline"));
    } finally { setSaving(false); }
  }

  function restoreVersion(v: NonNullable<BrandData["history"]>[0]) {
    setSp(v.system_prompt ?? ""); setSg(v.style_guide ?? "");
    setFw((v.forbidden_words ?? []).join(", "));
    markDirty(); setViewingV(v.version);
  }

  // AA-741 (S229): a .docx brief is READ and pre-fills the form for review (nothing is saved until
  // the tenant presses Save). Before, the upload only stored the file and the form stayed empty.
  // The original file is still archived; a PDF is archived only (automatic reading is .docx).
  async function handleUpload(file: File) {
    setUploadStatus("uploading"); setBriefNotes([]);
    const isDocx = file.name.toLowerCase().endsWith(".docx");
    try {
      if (file.size > 5 * 1024 * 1024) {
        setUploadStatus("error"); setBriefNotes([t("tooLarge")]);
        return;
      }
      // Sent as base64 JSON, not multipart: the API Gateway in front of the API corrupts binary
      // multipart bodies (AA-741, S229).
      const file_base64 = await toBase64(file);
      const payload = { filename: file.name, content_type: file.type || (isDocx
        ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/pdf"),
        file_base64 };
      const stored = fetch("/api/tenant/admin/brand-identity/upload", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      }).catch(() => null);
      if (!isDocx) {
        const r = await stored;
        setUploadStatus(r?.ok ? "stored" : "error");
        return;
      }
      const r = await fetch("/api/tenant/admin/brand-identity/parse", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, file_base64 }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setUploadStatus("error");
        setBriefNotes([typeof body.detail === "string" ? body.detail : t("readFailed")]);
        return;
      }
      const f = body.fields ?? {};
      if (f.brand_name) setBrandName(f.brand_name);
      if (f.brand_type) setBrandType(f.brand_type);
      if (f.core_idea) setCoreIdea(f.core_idea);
      if (f.target_markets?.length) setTargetMarkets(f.target_markets.join(", "));
      if (f.customer_segment) setCustomerSegment(f.customer_segment);
      if (f.customer_mindset) setCustomerMindset(f.customer_mindset);
      if (f.tone_of_voice?.length) setToneOfVoice(f.tone_of_voice.join(", "));
      if (f.style_guide) setSg(f.style_guide);
      if (f.good_examples) setGoodExamples(f.good_examples);
      if (f.forbidden_words?.length) setFw(f.forbidden_words.join(", "));
      // AA-1080: the brief now fills Brand context, the default CTA and the signature words too.
      if (f.system_prompt) setSp(f.system_prompt);
      if (f.default_cta) setDefaultCta(f.default_cta);
      if (f.signature_words?.length) setSigWords(f.signature_words.join(", "));
      const notes: string[] = [...(body.warnings ?? [])];
      const drafted = draftedFields(body.drafted);
      if (drafted.length) {
        const label = { default_cta: t("cta"), system_prompt: t("context"), signature_words: t("signature") };
        notes.push(t("drafted", drafted.map(d => label[d]).join(", ")));
      }
      const m = marketNoteLines(body.markets);
      for (const u of m?.unsupported ?? []) notes.push(t("marketNoteReason", u.country, u.uses));
      if (m?.unknown?.length) notes.push(t("notMarket", m.unknown.join(", ")));
      setBriefNotes(notes);
      setUploadStatus("done");
      markDirty();
      await stored;               // keep the original file archived
    } catch { setUploadStatus("error"); }
  }

  const wordCount = fw.split(",").map(w => w.trim()).filter(Boolean).length;

  return (
    <div style={{ display: "grid", gridTemplateColumns: showHistory ? "1fr 300px" : "1fr", gap: 24, alignItems: "start" }}>

      {/* MAIN FORM */}
      <div>
        {/* Header — AA-760: wraps on a phone (the History button squeezed the subtitle to ~235 px). */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap", marginBottom: 22 }}>
          <div style={{ flex: "1 1 280px", minWidth: 0 }}>
            <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: K.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>{t("title")}</h2>
            <p style={{ fontSize: 13, color: K.muted, lineHeight: 1.6, margin: 0 }}>
              {t("sub")}
            </p>
          </div>
          {data?.version && (
            <Button variant="ghost" size="sm" onClick={() => setShowHistory(!showHistory)} aria-expanded={showHistory}
              style={{ flexShrink: 0 }}>
              <History size={13} /> {t("history", data.version)}
            </Button>
          )}
        </div>

        {/* Active version indicator */}
        {data?.configured && data?.version && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", marginBottom: 22, background: K.successBg2, border: `1px solid ${K.successBorder}`, borderRadius: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: K.success, flexShrink: 0 }} />
            <span style={{ fontSize: 12.5, color: K.success, fontWeight: 600 }}>{t("activeVersion", data.version)}</span>
            {data.updated_at && <span style={{ fontSize: 11, color: K.muted, marginLeft: "auto" }}>{t("lastUpdated", fmtDate(data.updated_at, lang))}</span>}
          </div>
        )}

        {/* Viewing old version banner */}
        {viewingV !== null && (
          <div style={{ padding: "10px 14px", marginBottom: 18, background: K.amberSoft, border: `1px solid ${K.accentSoft}`, borderRadius: 8, display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 13, color: K.amber }}>{t("viewing", viewingV)}</span>
            <LinkButton tone="inherit" size="sm" onClick={onReset} style={{ marginLeft: "auto", color: K.amber }}>{t("backToActive")}</LinkButton>
          </div>
        )}

        {/* Fields — AA-557 J.23: extended to match Admin's own /admin/brand page structure. */}
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: 22, marginBottom: 16 }}>
          <TextField label={t("brandName")} value={brandName} onChange={v => { setBrandName(v); markDirty(); }}
            placeholder={t("brandNamePh")} />
          <TextField label={t("brandType")} value={brandType} onChange={v => { setBrandType(v); markDirty(); }}
            placeholder={t("brandTypePh")} />
          <AreaField
            label={t("coreIdea")} rows={2}
            placeholder={t("coreIdeaPh")}
            value={coreIdea} onChange={v => { setCoreIdea(v); markDirty(); }}
          />
          <TagListField label={t("targetMarkets")} value={targetMarkets} onChange={v => { setTargetMarkets(v); markDirty(); }}
            placeholder={t("targetMarketsPh")} hint={t("targetMarketsHint")} />
          <MarketNoteBox summary={marketNote ?? data.markets ?? null} />
          <AreaField
            label={t("segment")} rows={2}
            placeholder={t("segmentPh")}
            value={customerSegment} onChange={v => { setCustomerSegment(v); markDirty(); }}
          />
          <AreaField
            label={t("mindset")} rows={2}
            placeholder={t("mindsetPh")}
            value={customerMindset} onChange={v => { setCustomerMindset(v); markDirty(); }}
          />
          <TagListField label={t("tone")} value={toneOfVoice} onChange={v => { setToneOfVoice(v); markDirty(); }}
            placeholder={t("tonePh")} />
          <AreaField
            label={t("style")} rows={4}
            placeholder={t("stylePh")}
            value={sg} onChange={v => { setSg(v); markDirty(); }}
          />
          <AreaField
            label={t("examples")} rows={3}
            placeholder={t("examplesPh")}
            value={goodExamples} onChange={v => { setGoodExamples(v); markDirty(); }}
          />
          <TextField label={t("cta")} value={defaultCta}
            onChange={v => { setDefaultCta(v); markDirty(); }}
            placeholder={t("ctaPh")} />
          <CtaReviewNotice warning={liveCtaWarning ?? savedCtaWarning} style={{ marginTop: -6, marginBottom: 16 }} />
          <TagListField label={t("signature")} value={sigWords} onChange={v => { setSigWords(v); markDirty(); }}
            placeholder={t("signaturePh")} hint={t("signatureHint")} />
          <AreaField
            label={t("context")} rows={5}
            placeholder={t("contextPh")}
            value={sp} onChange={v => { setSp(v); markDirty(); }}
            hint={<span style={{ color: sp.length > 1800 ? K.danger : undefined }}>{`${sp.length}/2000`}</span>}
          />
          <Field
            label={<span style={{ display: "flex", justifyContent: "space-between", gap: 8 }}><span>{t("forbidden")}</span><span style={{ fontWeight: 400, color: K.muted2 }}>{t("words", wordCount)}</span></span>}
            hint={t("forbiddenHint")}
          >
            <Input value={fw} onChange={e => { setFw(e.target.value); markDirty(); }} placeholder={t("forbiddenPh")} />
          </Field>
        </div>

        {/* Preview */}
        {(sp || sg || fw) && (
          <div style={{ background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
            <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted, marginBottom: 10 }}>{t("howWeUse")}</div>
            <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.7 }}>
              {sp && <div style={{ marginBottom: 5 }}><strong style={{ color: K.ink }}>{t("contextLabel")}</strong> {sp.slice(0, 120)}{sp.length > 120 ? "…" : ""}</div>}
              {sg && <div style={{ marginBottom: 5 }}><strong style={{ color: K.ink }}>{t("styleLabel")}</strong> {sg.slice(0, 80)}{sg.length > 80 ? "…" : ""}</div>}
              {fw && (
                <div>
                  <strong style={{ color: K.ink }}>{t("willAvoid")}</strong>{" "}
                  {fw.split(",").filter(Boolean).map(w => w.trim()).slice(0, 8).map(w => (
                    <span key={w} style={{ display: "inline-block", margin: "1px 3px", padding: "1px 7px", borderRadius: 20, fontSize: 11, background: K.dangerSoft, color: K.danger }}>{w}</span>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Upload */}
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: 22, marginBottom: 22 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted, marginBottom: 14 }}>{t("upload")}</div>
          <input type="file" ref={fileRef} accept=".pdf,.docx" style={{ display: "none" }}
            onChange={e => { const f = e.target.files?.[0]; if (f) handleUpload(f); }} />
          <div onClick={() => fileRef.current?.click()}
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) handleUpload(f); }}
            style={{ border: `2px dashed ${K.accentSoft}`, borderRadius: 8, background: K.accentTint, padding: "28px 20px", textAlign: "center", cursor: "pointer" }}>
            <Upload size={24} color={K.accent} style={{ margin: "0 auto 10px" }} />
            <div style={{ fontSize: 13.5, color: K.ink, fontWeight: 500 }}>
              {uploadStatus === "uploading" ? t("uploading") :
               uploadStatus === "done"      ? t("briefRead") :
               uploadStatus === "stored"    ? t("pdfStored") :
               uploadStatus === "error"     ? t("uploadFailed") :
               t("drag")}
            </div>
            <div style={{ fontSize: 11, color: K.muted2, fontFamily: mono, marginTop: 6 }}>{t("uploadHint")}</div>
          </div>
        </div>

        {briefNotes.length > 0 && (
          <div role="status" style={{ padding: "12px 14px", marginBottom: 16, background: K.amberSoft, border: `1px solid ${K.accentSoft}`, borderRadius: 8 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: K.amber, marginBottom: 6 }}>{t("checkBefore")}</div>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.body, lineHeight: 1.6 }}>
              {briefNotes.map(n => <li key={n}>{n}</li>)}
            </ul>
          </div>
        )}

        {/* Save row */}
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <Button variant="primary" size="lg" disabled={saving || !dirty} onClick={save}>
            {saving ? t("saving") : t("saveVersion")}
          </Button>
          {saved && <span style={{ fontSize: 13, color: K.success, fontWeight: 600 }}><Check size={14} style={{ display: "inline" }} /> {t("saved")}</span>}
          {dirty && !saved && !saveError && <span style={{ fontSize: 13, color: K.amber }}>{t("unsaved")}</span>}
          {saveError && <span role="alert" style={{ fontSize: 13, color: K.danger }}>{saveError}</span>}
          {saved && marketNote && (
            <span style={{ fontSize: 12.5, color: K.muted, width: "100%" }}>
              {t("marketsInUse", marketNote.countries.filter(c => !marketNote.unsupported.includes(c)).join(", ") || t("usDefault"))}
              {marketNote.unsupported.length > 0 && t("researchNotYet", marketNote.unsupported.join(", "))}
            </span>
          )}
        </div>
      </div>

      {/* VERSION HISTORY SIDEBAR */}
      {showHistory && data?.history && (
        <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, overflow: "hidden", position: "sticky", top: 20 }}>
          <div style={{ padding: "14px 16px", borderBottom: `1px solid ${K.line}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("versionHistory")}</div>
            <IconButton variant="plain" size="sm" onClick={() => setShowHistory(false)} label={t("closeHistory")} title={t("close")}><span aria-hidden style={{ fontSize: 18 }}>×</span></IconButton>
          </div>
          <div style={{ padding: 14, maxHeight: "70vh", overflowY: "auto" }}>
            {data.history.map(h => (
              <div key={h.version} style={{ borderRadius: 8, marginBottom: 10, background: h.is_active ? K.successBg2 : K.bg, border: `1px solid ${h.is_active ? K.successBorder : K.line}`, overflow: "hidden" }}>
                <div style={{ padding: "10px 12px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <span style={{ fontSize: 14, fontWeight: 700, color: h.is_active ? K.success : K.ink }}>v{h.version}</span>
                    {h.is_active && <span style={{ marginLeft: 6, fontSize: 9, fontWeight: 700, color: K.success, background: K.successSoft, padding: "1px 6px", borderRadius: 10 }}>{t("active")}</span>}
                    <div style={{ fontSize: 10.5, color: K.muted2, marginTop: 3, fontFamily: mono }}>{fmtDate(h.updated_at, lang)}</div>
                  </div>
                  {!h.is_active && (
                    <Button size="sm" variant="secondary" onClick={() => restoreVersion(h)}>
                      <RotateCcw size={10} /> {t("restore")}
                    </Button>
                  )}
                </div>
                {h.system_prompt && (
                  <div style={{ padding: "0 12px 10px", borderTop: `1px solid ${K.line}`, paddingTop: 8 }}>
                    <div style={{ fontSize: 10.5, color: K.muted2, marginBottom: 3 }}>{t("context")}</div>
                    <div style={{ fontSize: 11.5, color: K.body, lineHeight: 1.5 }}>
                      {h.system_prompt.slice(0, 100)}{h.system_prompt.length > 100 ? "…" : ""}
                    </div>
                    {h.forbidden_words?.length > 0 && (
                      <div style={{ marginTop: 6 }}>
                        <div style={{ fontSize: 10, color: K.muted2, marginBottom: 3 }}>{t("forbiddenShort")}</div>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
                          {h.forbidden_words.slice(0, 5).map(w => (
                            <span key={w} style={{ fontSize: 10, padding: "1px 7px", background: K.dangerSoft, color: K.danger, borderRadius: 20 }}>{w}</span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
            <div style={{ fontSize: 11, color: K.muted2, lineHeight: 1.5, padding: "0 4px", marginTop: 4 }}>
              {t("restoreHelp1")} <strong>{t("restore")}</strong> {t("restoreHelp2")}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
