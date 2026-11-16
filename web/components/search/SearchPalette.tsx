"use client";
// web/components/search/SearchPalette.tsx
// AA-963 (CRM-32, design v3 §2.3 A-00, R35) — the agency app's global search: the ⌘K / Ctrl K palette of the CRM
// shell (kit Modal + ListButton, like the studio's palette). The term goes to POST /bff/search in the body (never a
// URL), debounced; the API answers the kinds the member may see (role matrix + feature keys) and finds clients by a
// whole e-mail, phone or name words only. A refusal (a partial e-mail or phone, a wildcard) is explained, not hidden.
// Content pieces (CIS) come from the studio BFF the person already uses, only when CIS lists this agency among their
// studios (`contentEnabled`) and the studio token is this agency's (`tenantId`), filtered with the API's accent fold.
// Every answer names the term it belongs to (AA-963 review): while the debounce has not sent the term typed now, the
// previous term's results are not shown, so Enter never opens a record that does not match the visible term.

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { CornerDownLeft, Search } from "lucide-react";
import { K, ListButton, Modal, RADIUS, alpha } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { fmt, type MessageKey } from "@/lib/i18n";
import {
  matchContentPieces,
  noticeOf,
  paletteGroups,
  refusalOf,
  shouldSearch,
  statusFor,
  type ContentPieceHit,
  type PaletteRow,
  type SearchConfig,
  type SearchResult,
  type SearchStatus,
} from "@/lib/search";
import { loadContentPieces } from "@/lib/searchContent";

type Dict = Record<MessageKey, string>;

const DEBOUNCE_MS = 250;

export function SearchPalette(props: {
  open: boolean;
  onClose: () => void;
  dict: Dict;
  config: SearchConfig;
  contentEnabled: boolean;
  tenantId: string;
  lang: "en" | "vi";
}) {
  if (!props.open) return null;
  return <Palette {...props} />;
}

/** The search status of the term typed now (:func:`statusFor`: an earlier term's answer is never returned). */
function useSearch(term: string, config: SearchConfig): SearchStatus {
  const [status, setStatus] = useState<SearchStatus>({ kind: "idle" });
  useEffect(() => {
    if (!shouldSearch(term, config)) return;
    const q = term.trim();
    let live = true;
    const id = window.setTimeout(async () => {
      const res = await bffSend<SearchResult>("POST", "search", { q });
      if (!live) return;
      if (res.ok) setStatus({ kind: "done", term: q, result: res.data });
      else if (!res.redirecting) {
        const refusal = refusalOf(res.detail);
        setStatus({ kind: "error", term: q, key: refusal ? `search.refusal.${refusal}` : "search.error" });
      }
    }, DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(id);
    };
  }, [term, config]);
  return statusFor(status, term, config);
}

function useContentPieces(enabled: boolean, tenantId: string, lang: "en" | "vi"): ContentPieceHit[] {
  const [pieces, setPieces] = useState<ContentPieceHit[]>([]);
  const session = useRef<object>({}); // one per palette open: the pieces are read again at the next open
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    void loadContentPieces(tenantId, lang, session.current).then((list) => {
      if (live) setPieces(list);
    });
    return () => {
      live = false;
    };
  }, [enabled, tenantId, lang]);
  return pieces;
}

function Palette({ onClose, dict, config, contentEnabled, tenantId, lang }: Parameters<typeof SearchPalette>[0]) {
  const router = useRouter();
  const [term, setTerm] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const status = useSearch(term, config);
  const wantsContent = contentEnabled && term.trim().length >= Math.max(1, config.min_text_chars);
  const pieces = useContentPieces(wantsContent, tenantId, lang);

  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, []);

  const result = status.kind === "done" ? status.result : null;
  const groups = useMemo(() => {
    const content = wantsContent ? matchContentPieces(pieces, term, config.results_per_kind) : [];
    return paletteGroups(result, content, dict["search.archived"]);
  }, [result, pieces, term, wantsContent, config.results_per_kind, dict]);
  const notice = noticeOf(result);
  const flat = useMemo(() => groups.flatMap((g) => g.rows), [groups]);
  const safeSelected = flat.length === 0 ? 0 : Math.min(selected, flat.length - 1);
  const position = useMemo(() => new Map(flat.map((row, i) => [row.key, i])), [flat]);

  function activate(row: PaletteRow | undefined) {
    if (!row) return;
    onClose();
    if (row.external) window.location.assign(row.href);
    else router.push(row.href);
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, Math.max(0, flat.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      activate(flat[safeSelected]);
    }
  }

  return (
    <Modal open onClose={onClose} width={600} ariaLabel={dict["search.aria"]} testId="crm-search-palette">
      <div style={{ margin: "-18px -20px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: `1px solid ${K.line}` }}>
          <Search size={18} aria-hidden style={{ color: K.muted2, flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={term}
            maxLength={config.query_max_chars > 0 ? config.query_max_chars + 1 : undefined}
            onChange={(e) => {
              setTerm(e.target.value);
              setSelected(0);
            }}
            onKeyDown={onKey}
            placeholder={dict["search.placeholder"]}
            aria-label={dict["search.aria"]}
            aria-controls="crm-search-results"
            autoComplete="off"
            data-testid="crm-search-input"
            style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", fontSize: 15, color: K.ink }}
          />
        </div>
        <div id="crm-search-results" role="listbox" aria-label={dict["search.results"]} style={{ maxHeight: "min(440px, 60vh)", overflowY: "auto", padding: 6 }}>
          <StatusLine status={status} term={term} config={config} dict={dict} empty={flat.length === 0} />
          {notice && (
            <div role="note" data-testid="crm-search-notice" style={{ padding: "8px 12px", fontSize: 12, color: K.muted }}>
              {dict[`search.notice.${notice}` as MessageKey]}
            </div>
          )}
          {groups.map((g) => (
            <div key={g.kind} role="group" aria-label={dict[`search.kind.${g.kind}` as MessageKey]} style={{ marginBottom: 4 }}>
              <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted2, padding: "8px 10px 4px" }}>
                {dict[`search.kind.${g.kind}` as MessageKey]}
              </div>
              {g.rows.map((row) => {
                const idx = position.get(row.key) ?? 0;
                const isSel = idx === safeSelected;
                return (
                  <ListButton
                    key={row.key}
                    role="option"
                    selected={isSel}
                    onClick={() => activate(row)}
                    onMouseEnter={() => setSelected(idx)}
                    testId={`crm-search-row-${g.kind}`}
                    style={{ gap: 10, padding: "9px 10px", borderRadius: RADIUS.md, fontWeight: 400, background: isSel ? alpha(K.accent, 12) : "transparent" }}
                  >
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span style={{ display: "block", fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.label}</span>
                      {row.sub && (
                        <span style={{ display: "block", fontSize: 11.5, color: K.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.sub}</span>
                      )}
                    </span>
                    {isSel && <CornerDownLeft size={14} aria-hidden style={{ color: K.muted2, flexShrink: 0 }} />}
                  </ListButton>
                );
              })}
            </div>
          ))}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 14, padding: "8px 14px", borderTop: `1px solid ${K.line}`, fontSize: 11, color: K.muted2 }}>
          <span>{dict["search.keys.move"]}</span>
          <span>{dict["search.keys.open"]}</span>
          <span>{dict["search.keys.close"]}</span>
        </div>
      </div>
    </Modal>
  );
}

function StatusLine({ status, term, config, dict, empty }: { status: SearchStatus; term: string; config: SearchConfig; dict: Dict; empty: boolean }) {
  const style = { padding: "10px 12px", fontSize: 12.5, color: K.muted } as const;
  if (status.kind === "error") {
    return (
      <div role="alert" data-testid="crm-search-refusal" style={{ ...style, color: K.ink }}>
        {dict[status.key as MessageKey]}
      </div>
    );
  }
  if (status.kind === "loading") return <div role="status" data-testid="crm-search-loading" style={style}>{dict["search.searching"]}</div>;
  if (!term.trim()) return <div style={style}>{dict["search.hint"]}</div>;
  if (status.kind === "idle" && !shouldSearch(term, config)) {
    return <div style={style}>{fmt(dict["search.tooShort"], { n: Math.max(1, config.min_text_chars) })}</div>;
  }
  if (status.kind === "done" && empty) return <div style={style}>{dict["search.noResults"]}</div>;
  return null;
}
